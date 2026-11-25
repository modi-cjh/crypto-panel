#!/usr/bin/env python3
"""Mirror the combined paper account onto an OKX account: demo trading (default) or, explicitly, the live account.

Mode switch (environment, read once per process; see trading_mode / dry_run_forced):
  OKX_TRADING_MODE=demo (default)  every request carries `x-simulated-trading: 1`; state in <data>/okx_demo/.
  OKX_TRADING_MODE=live            no simulated header (real account); state in <data>/okx_live/. Refused inside
                                   GitHub Actions and the runtime/ service; only follows evolve/signals.json.
  OKX_DRY_RUN                      live: dry-run unless exactly `0` (orders are never sent by default);
                                   demo: `1` forces dry-run, unset keeps the existing behaviour.
In dry-run the OKX client itself refuses every non-GET request, so no code path can place, amend or close an order.

The paper trader (tools/paper_trader.py) decides; this script executes those decisions on OKX's demo
environment (or, in live mode, on the real account) so that real order handling, fills, slippage and exchange-side
stops can be compared with the paper results. In demo mode every request carries `x-simulated-trading: 1`.

Each run, against <data>/combined/state.json:
  - a paper position opened in the last OPEN_WINDOW that is not mirrored yet is opened at market,
    sized by the strategy's risk at the paper stop (evolved mode: cross margin, the strategy's own 5–10 % risk gene;
    half from the 10th position already open on that side; the legacy modes: cross, 1 %), with the stop (and the
    mean-reversion target) attached as exchange-side algo orders;
  - exits (2026-11-21, the same for every strategy; execution.py EXIT RULES): every mirrored position keeps BOTH an
    exchange stop and an exchange take-profit (a missing take-profit is placed again at the paper target, else at
    entry ± DEFAULT_TARGET_RR × stop distance) — except a NO-TARGET position (2026-11-25, execution EXIT RULES 5: the
    take-profit would be ≤ 0 / not a valid price; signals 'no_target' / tp None, see target_of()): it opens with the
    stop only (no TP fields in attachAlgoOrds), is stored with target None / no_target True, never gets a TP placed or
    amended later, and exits by the stop / trailing stop / paper close only; the trailing stop / breakeven (execution TRAIL & RISK RULES a:
    execution.trail_stop_level with R = trail_unit(paper dist, live dist) = the smaller of the paper stop distance and
    |live entry − live initial stop|, from OKX's closed 1H candles since the entry, or the paper's own trailed stop when
    that is tighter) is applied by amending the exchange stop once per run, only in the position's favour. Granularity: paper moves the stop on every 1H
    bar, this program once per run (hourly at :50 UTC on the live server), so the exchange stop can lag by up to an hour;
  - when the paper position is gone (paper stop / target / reverse signal / trailing stop), the mirrored position is
    closed with close-position + autoCxl (its resting algos are cancelled); the paper's reason, published in
    signals.json 'exits', is kept as closed_reason (reverse_signal, trailing_stop, manual_user, else paper_closed). Reverse signals are
    seen when signals.json is republished (every 4 h), so this close can come up to ~4 h after the paper close;
  - if the exchange stop already closed a mirrored position, it is marked done and never reopened.
Safety: a new mirror starts paused (check with `dry-run`, then `resume`); positions not opened by this script are never touched; in demo mode
the process refuses to send any request without `x-simulated-trading: 1`. Evolved mode uses cross margin (全仓), the strategy's own risk (5–10 %) and leverage
(1–20×, clamped to that coin's OKX max; fallback 20× if the public spec is missing). A new order is skipped if its stop would leave account equity at or below total maintenance margin. A UTC+7 day
loss of 10% closes everything and blocks new entries until the next day; actions `pause`, `resume`,
`flatten` (close all and pause) and `dry-run` (log intended orders, send nothing) are available.
Secrets come only from OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE (environment), reach curl through stdin and are
never printed, logged or written to any file.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from volume_test import profile as volume_profile, describe as describe_volume
from runtime_guard import entries_allowed, atomic_json

sys.path.insert(0, str(Path(__file__).resolve().parent / 'evolve'))
from execution import LEVERAGE as GENE_MAX_LEVERAGE, clamp_leverage, cross_open_allowed, DEFAULT_LEVERAGE, maintenance_rate, MAJORS, FEE, MAX_SAME_SIDE_POSITIONS, same_side_full  # noqa: E402
from execution import DEFAULT_TARGET_RR, TRAIL_START_R, TRAIL_DIST_R, TRAIL, trail_stop_level, trail_unit  # noqa: E402
from execution import Series, target_rr, take_profit  # noqa: E402

BASE = 'https://www.okx.com'
INSTRUMENTS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP']
RISK, MAX_LEVERAGE, DAILY_HALT = .01, 10, .10   # legacy volume-test profiles: 1 % risk, 10× cap; daily halt 10 %
EVOLVED_DAILY_HALT = .25                         # following the evolution champions: 25 % (2026-11-15, was 10 %), counted on the whole account including the leader
RISK_RANGE = (.05, .10)   # evolved mode: each order risks its strategy's own risk gene, kept inside this range; missing p['risk'] defaults to 5 %
MAX_TRADE_RISK = .10      # 2026-11-20: lowered from .20 (PR #106 had raised it from .10); paper/evolution execution.MAX_TRADE_RISK is .10 as well (synced)
EVOLVED_MAX_LEVERAGE = GENE_MAX_LEVERAGE   # 20×, same cap as the evolve leverage gene
# MAJORS comes from execution. The per-instrument OKX leverage cap is read from the public instruments spec; when it is
# missing or unusable no new position is opened on that coin (fail closed, no 20× fallback). Existing positions are
# still managed and closed.
# Every seat including the leader uses its own risk and leverage genes (5–10 %, 1–20×). Majors may be doubled then
# clamped so the result never exceeds 10 %. The leader counts toward the same-direction cap and is subject to the
# daily-loss stop, same as everyone else. LEADER_RISK / LEADER_LEVERAGE are the caps, not a fixed bigger size.
# LEADER_RISK follows MAX_TRADE_RISK: 10 % (PR #106 had lifted it to 20 %; the later 2026-11-20 change set it back to 10 %).
LEADER_RISK, LEADER_LEVERAGE = MAX_TRADE_RISK, EVOLVED_MAX_LEVERAGE
MARGIN_MODE = 'cross'             # evolved and legacy modes both use cross margin (全仓); liquidation is at the account level
# Trading mode switch. demo = OKX demo trading (x-simulated-trading: 1); live = the real account (header omitted).
MODE_ENV, DRY_RUN_ENV = 'OKX_TRADING_MODE', 'OKX_DRY_RUN'
MODES = ('demo', 'live')
STATE_DIRS = {'demo': 'okx_demo', 'live': 'okx_live'}   # live never shares (or inherits) the demo mirror's state
SIMULATED_HEADER = 'x-simulated-trading'
SIMULATED_VALUE = '1'
# Signal timing (env OKX_SIGNAL_MODE, read in main() via signal_timing(); live and demo use the same function):
#   github (default, unset)  signals.json from the GitHub run every 4 h (+ up to ~3 h cron delay): 9 h / 5 h, unchanged.
#   hourly                   signals.json from the live server's tools/evolve/signals_hourly.py at H:01:30 every hour:
#                            2 h 15 min / 2 h (one missed hour still passes; older entries go through late_ok).
# OKX_LIVE_SIGNAL_MAX_AGE_MIN / OKX_OPEN_WINDOW_MIN override either value (minutes, 1..1440).
# The hourly defaults only apply when the mode says so: under the GitHub 4 h signals 2 h 15 min would refuse most hours.
SIGNAL_TIMING = {'github': (9 * 3600_000, 5 * 3600_000), 'hourly': (135 * 60_000, 2 * 3600_000)}
SIGNAL_MODE_ENV, MAX_AGE_ENV, OPEN_WINDOW_ENV = 'OKX_SIGNAL_MODE', 'OKX_LIVE_SIGNAL_MAX_AGE_MIN', 'OKX_OPEN_WINDOW_MIN'
LIVE_SIGNAL_MAX_AGE = SIGNAL_TIMING['github'][0]   # live refuses to act on evolve/signals.json older than this
MAJOR_FACTOR = 2.0   # majors (execution.MAJORS) risk twice as much (execution.ACCOUNT_RULES)
PRICE_TOLERANCE = .01             # demo mode: no entry while the demo price is further than this from the real market (data.PRICE_TOLERANCE)
CROWD_STEPS = ((10, .5),)  # 2026-11-20: <10 same-side already open → full risk; ≥10 → half. Paper/evolution execution.CROWD_STEPS is the same (synced).
SAME_SIDE_CAP = .05
MAX_SAME_SIDE = MAX_SAME_SIDE_POSITIONS  # evolved mode: at most 20 open mirrored positions per side (execution.MAX_SAME_SIDE_POSITIONS, same count as the paper account)
HISTORY_POINTS = 3000            # equity curve points kept (~31 days at one point per 15 minutes)
HISTORY_STEP = 15 * 60_000       # runs every 5 minutes; keep one curve point per 15 minutes
OPEN_WINDOW = SIGNAL_TIMING['github'][1]   # paper entries at most this old are mirrored as they are; older ones only via late_ok
LATE_BAND = (.5, 1.5)             # older entries are still followed while the price is within this band of the paper stop distance
MIN_STOP_DIST = .002              # skip entries whose stop is already too close to the current price
AMEND_TOLERANCE = .003            # amend the exchange stop when the paper stop moved more than 0.3% (0.1% logged a move every run)
UTC7 = timezone(timedelta(hours=7))
# Paper close reason (signals.json 'exits') → closed_reason of the mirrored position; anything else is paper_closed.
PAPER_EXIT_REASONS = {'reverse_signal': 'reverse_signal', 'trailing_stop': 'trailing_stop', 'manual_user': 'manual_user'}   # manual_user: paper.MANUAL_CLOSES
# Which resting exchange order closed a mirror (outcome()) → closed_reason.
EXCHANGE_EXIT_REASONS = {'target': 'take_profit', 'stop': 'stop_loss', 'liquidation': 'liquidation'}
TAG = {'mean_reversion': 'mr', 'breakout_long': 'bl', 'breakout_daily': 'bd', 'evolved': 'ev'}
EVOLVED_INSTRUMENTS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP']
# PRICE_OFF LIVE (2026-11-25): signals.json 'price_off_live' = qualified signals the paper account refused ONLY because the
# coin's OKX DEMO price was off the real market (data.price_check). Followed by the LIVE trader only (Mirror.follow_price_off,
# set in main() for live; demo keeps its own demo_price_off check and ignores the list). Stop / take-profit are never
# copied: real_levels() recomputes them from REAL OKX candles of the genome's timeframe at the signal bar, at the live
# entry price (stop = px ∓ stop_atr × ATR14, tp = px ± target_rr × that distance, execution.target_rr; no TP when that is
# ≤ 0, no entry when the stop is ≤ 0). Exits: the
# exchange stop / take-profit, the trailing stop from the live entry, and the strategy's own exits published in
# 'price_off_live_exits' (the entry leaves the list → closed at market, as a paper close closes a mirror). A signals.json
# WITHOUT the field (older publisher) opens nothing new and closes nothing: those positions keep their exchange orders.
PRICE_OFF_LIVE_BAR = {1: '1H', 4: '4H', 24: '1Dutc'}   # OKX bar per genome tf; 1Dutc = UTC-aligned days, like evolve's 1D
PRICE_OFF_LIVE_MIN_BARS = 30                            # fewer closed real candles than this: no ATR, no entry
SCAN_LIMIT = 50           # coins scanned for new entries: the first 50 of the evolution's list (most traded first); same as evolve.data.UNIVERSE_SIZE
ACTIVE_PROFILE = None


def configure_evolved(universe=None):
    """Following the evolution champion (tools/evolve): the old strategy profiles no longer apply. `universe` is the
    coin list the evolution trades (signals.json 'instruments', most traded first), cut to its first SCAN_LIMIT coins
    so a longer list from an older evolution run is never scanned in full; without it, the five majors."""
    global INSTRUMENTS, RISK, SAME_SIDE_CAP, ACTIVE_PROFILE, DAILY_HALT, MARGIN_MODE
    MARGIN_MODE = 'cross'
    # Each order risks its strategy's own risk gene (5–10 %, default 5 % when missing, capped at 10 % after crowding and the major-coin factor)
    # and its leverage gene (1–20×, clamped to that coin's OKX max). An order is skipped if its stop would leave
    # account equity at or below total maintenance margin (0.5 % majors / 2.5 % others).
    # No total-risk cap per direction (only at most MAX_SAME_SIDE = 20 open per side); a 25 % daily loss on the whole account closes every
    # position until tomorrow. The leader uses the same gene ranges; the title only decides signal priority.
    INSTRUMENTS, RISK, SAME_SIDE_CAP, DAILY_HALT = list(EVOLVED_INSTRUMENTS), RISK_RANGE[0], None, EVOLVED_DAILY_HALT
    if isinstance(universe, list) and universe and len(set(universe)) == len(universe) and all(
            isinstance(i, str) and re.fullmatch(r'[A-Z0-9]+-USDT-SWAP', i) for i in universe):
        INSTRUMENTS = list(universe)[:SCAN_LIMIT]
    ACTIVE_PROFILE = dict(mode='evolved', active=False, until=None, instruments=list(INSTRUMENTS), scan_limit=len(INSTRUMENTS), risk=RISK, risk_max=RISK_RANGE[1], same_side_cap=SAME_SIDE_CAP, daily_halt=DAILY_HALT,
                          leader_risk=LEADER_RISK, leader_leverage=LEADER_LEVERAGE, max_risk=MAX_TRADE_RISK, max_leverage=EVOLVED_MAX_LEVERAGE,
                          fallback_leverage=None, margin_mode=MARGIN_MODE, majors=list(MAJORS), major_factor=MAJOR_FACTOR,
                          max_same_side=MAX_SAME_SIDE)
    return ACTIVE_PROFILE


def configure_volume_test(now=None, universe=None):
    global INSTRUMENTS, RISK, SAME_SIDE_CAP, DAILY_HALT, MARGIN_MODE
    DAILY_HALT, MARGIN_MODE = .10, 'cross'
    p = volume_profile(now)
    INSTRUMENTS, RISK = p['instruments'], p['risk']
    if p['mode'] == 'aggressive' and isinstance(universe, list) and len(universe) == p['scan_limit'] and len(set(universe)) == len(universe) and all(isinstance(i,str) and re.fullmatch(r'[A-Z0-9]+-USDT-SWAP',i) for i in universe):
        INSTRUMENTS = list(universe)
        p['instruments'] = INSTRUMENTS
    SAME_SIDE_CAP = p['same_side_cap']
    return p


def real_price(inst):
    """Last price on the real OKX market (public, no keys, no demo header), or None. The demo order book is a separate
    market: small coins there can sit far from the real price the strategies trade on."""
    try:
        out = subprocess.run(['curl', '-sS', '-m', '10', f'{BASE}/api/v5/market/ticker?instId={inst}'], capture_output=True, text=True).stdout
        return float(json.loads(out)['data'][0]['last']) or None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def signal_timing(env=None):
    """(signal mode, max signal age ms, open window ms) from OKX_SIGNAL_MODE and the two minute overrides. An unknown
    mode or a bad override refuses to run (fail closed) rather than trading on a window nobody chose."""
    env = os.environ if env is None else env
    mode = str(env.get(SIGNAL_MODE_ENV, '')).strip().lower() or 'github'
    if mode not in SIGNAL_TIMING:
        raise SystemExit(f'{SIGNAL_MODE_ENV} 只能是 github 或 hourly，已拒绝运行')
    age, window = SIGNAL_TIMING[mode]

    def minutes(key, default):
        raw = str(env.get(key, '')).strip()
        if not raw:
            return default
        try:
            v = float(raw)
        except ValueError:
            raise SystemExit(f'{key} 必须是分钟数，已拒绝运行') from None
        if not 1 <= v <= 1440:
            raise SystemExit(f'{key} 必须在 1–1440 分钟之间，已拒绝运行')
        return int(v * 60_000)
    return mode, minutes(MAX_AGE_ENV, age), minutes(OPEN_WINDOW_ENV, window)


def apply_signal_timing(env=None):
    global LIVE_SIGNAL_MAX_AGE, OPEN_WINDOW
    mode, LIVE_SIGNAL_MAX_AGE, OPEN_WINDOW = signal_timing(env)
    return mode, LIVE_SIGNAL_MAX_AGE, OPEN_WINDOW


def trading_mode(env=None):
    """demo (default) or live, from OKX_TRADING_MODE. Live is refused where it must never run: GitHub Actions (the key
    is bound to the Singapore server's IP, and the scheduled job is the demo mirror) and the runtime/ service (demo-only)."""
    env = os.environ if env is None else env
    mode = str(env.get(MODE_ENV, '')).strip().lower() or 'demo'
    if mode not in MODES:
        raise SystemExit(f'{MODE_ENV} 只能是 demo 或 live，已拒绝运行')
    if mode == 'live':
        if str(env.get('GITHUB_ACTIONS', '')).lower() == 'true':
            raise SystemExit('实盘只在绑定 IP 的服务器上运行：拒绝在 GitHub Actions 中以 live 模式启动')
        if env.get('PANEL_RUNTIME_CONTROL_FILE'):
            raise SystemExit('runtime/ 常驻服务仍只支持模拟盘；实盘请用 deploy/live 的 systemd 定时任务')
    return mode


def dry_run_forced(mode, env=None):
    """Live: dry-run unless OKX_DRY_RUN is exactly '0' (default on). Demo: dry-run only when OKX_DRY_RUN=1 (unchanged default)."""
    env = os.environ if env is None else env
    value = str(env.get(DRY_RUN_ENV, '')).strip()
    return value != '0' if mode == 'live' else value == '1'


def assert_mode(mode):
    """Refuse an unknown mode, or a demo mode whose simulated-trading flag has been tampered with."""
    if mode not in MODES:
        raise SystemExit('未知交易模式，已拒绝运行')
    if mode == 'demo' and (SIMULATED_HEADER != 'x-simulated-trading' or str(SIMULATED_VALUE) != '1'):
        raise SystemExit('模拟盘模式必须带 x-simulated-trading: 1，已拒绝运行')


def signed_headers(key, sign, ts, passphrase, mode):
    """Signed OKX headers. Demo always carries x-simulated-trading: 1; live never does (it would be rejected by a live key)."""
    assert_mode(mode)
    headers = {'OK-ACCESS-KEY': key, 'OK-ACCESS-SIGN': sign, 'OK-ACCESS-TIMESTAMP': ts, 'OK-ACCESS-PASSPHRASE': passphrase,
               'Content-Type': 'application/json'}
    if mode == 'demo':
        headers[SIMULATED_HEADER] = SIMULATED_VALUE
        if headers.get('x-simulated-trading') != '1':
            raise OKXError('demo: refused to send a request without x-simulated-trading: 1')
    elif any(k.lower() == 'x-simulated-trading' for k in headers):
        raise OKXError('live: the simulated-trading header must not be sent')
    return headers


def state_dir(data, mode):
    return Path(data) / STATE_DIRS[mode]


def load_state(path, mode):
    """The mirror's own state for this mode. A state written by the other mode is refused (live must never manage
    positions recorded by the demo mirror, and vice versa); demo states from before the switch carry no mode."""
    state = json.loads(Path(path).read_text()) if Path(path).exists() else new_state(mode)
    if state.get('mode', 'demo') != mode:
        raise SystemExit(f'{path} 属于 {state.get("mode", "demo")} 模式，当前为 {mode}，已拒绝运行')
    return state


def signal_age_ms(sig, now):
    """Age of the signal file in ms; None when updated_at is missing, unparsable or in the future (a future timestamp
    gives a negative age that would slip past the max-age check, so it is treated as an invalid signal)."""
    try:
        ts = datetime.fromisoformat(str(sig.get('updated_at')).replace('Z', '+00:00'))
        age = int((now - ts).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None
    return age if age >= 0 else None


class OKXError(RuntimeError):
    pass


class OKX:
    """Minimal signed client for OKX: demo trading (simulated header) or the live account, chosen by `mode`
    (default: OKX_TRADING_MODE). With dry_run (default: OKX_DRY_RUN, on for live) every non-GET request is refused
    here, below all trading logic, so a dry-run can read the account but never place, amend or close anything."""
    def __init__(self, key, secret, passphrase, curl='curl', attempts=3, mode=None, dry_run=None):
        self.key, self.secret, self.passphrase, self.curl = key, secret, passphrase, curl
        self.mode = trading_mode() if mode is None else mode
        assert_mode(self.mode)
        self.dry_run = dry_run_forced(self.mode) if dry_run is None else bool(dry_run)
        self.attempts = 1 if os.environ.get('PANEL_RUNTIME_CONTROL_FILE') else attempts

    def __repr__(self):   # never expose the credentials through repr / tracebacks
        return f'OKX(mode={self.mode!r}, dry_run={self.dry_run!r})'

    def request(self, method, path, body=None):
        if method != 'GET' and self.dry_run:
            raise OKXError(f'dry-run：已拒绝发送 {method} {path.split("?")[0]}（{self.mode}）')
        payload = json.dumps(body, separators=(',', ':')) if body is not None else ''
        ts = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        sign = base64.b64encode(hmac.new(self.secret.encode(), f'{ts}{method}{path}{payload}'.encode(), hashlib.sha256).digest()).decode()
        headers = signed_headers(self.key, sign, ts, self.passphrase, self.mode)
        config = ''.join(f'header = "{k}: {v}"\n' for k, v in headers.items()) + f'url = "{BASE}{path}"\nrequest = "{method}"\n'
        if payload:
            config += 'data = "' + payload.replace('\\', '\\\\').replace('"', '\\"') + '"\n'
        for attempt in range(self.attempts):
            timeout = '5' if os.environ.get('PANEL_RUNTIME_CONTROL_FILE') else '20'
            out = subprocess.run([self.curl, '-sS', '-m', timeout, '-K', '-'], input=config, capture_output=True, text=True)
            if out.returncode == 0:
                try:
                    return json.loads(out.stdout)
                except ValueError:
                    pass
            if attempt + 1 < self.attempts:
                time.sleep(1 + attempt)
        raise OKXError(f'{method} {path.split("?")[0]} failed')


def ok(resp):
    return resp.get('code') == '0'


def first_error(resp):
    d = (resp.get('data') or [{}])[0] if isinstance(resp.get('data'), list) else {}
    return f"{resp.get('code')} {resp.get('msg', '')} {d.get('sCode', '')} {d.get('sMsg', '')}".strip()


def quantize(value, step, rounding=ROUND_DOWN):
    step = Decimal(str(step))
    return (Decimal(str(value)) / step).to_integral_value(rounding) * step


def fmt(d):
    s = format(d, 'f')
    return s.rstrip('0').rstrip('.') if '.' in s else s


def local_day(now):
    return now.astimezone(UTC7).date().isoformat()


def new_state(mode='demo'):
    # Starts paused: run `dry-run` first, then `resume` to begin placing orders. A live state is stamped so that it can
    # never be confused with the demo mirror's (load_state).
    state = dict(mirrored={}, paused=True, halted=False, day=None, day_start=None)
    if mode != 'demo':
        state['mode'] = mode
    return state


def enrich_closes(mirror, state, actions):
    """Fill in realised PnL for closes logged without it (e.g. before this existed), from OKX position history.
    Results are cached in state so each close is looked up once."""
    state.pop('close_results', None); state.pop('close_results_v2', None)   # v2 could attach an older close of the same coin
    cache = state.setdefault('close_results_v3', {})
    for a in actions:
        if a.get('action') not in ('closed_on_exchange', 'close') or a.get('execution_test') or a.get('pnl') is not None:
            continue                                # (a pnl already there was read from the exchange when the close was logged)
        key = f"{a['ts']}|{a['inst']}"
        if key not in cache:
            ts = int(datetime.fromisoformat(a['ts'].replace('Z', '+00:00')).timestamp() * 1000)
            # The mirror this close belongs to: the latest one on that coin opened before the close (not the first ever).
            done = [m for m in state.get('mirrored', {}).values() if m.get('inst') == a['inst'] and m.get('status') == 'done'
                    and m.get('opened_at') and int(m['opened_at']) <= ts]
            m = max(done, key=lambda m: int(m['opened_at']), default=None)
            if not m:
                continue
            try:
                res = mirror.fill_result(a['inst'], m.get('posSide', 'net'), int(m['opened_at']), ts + 120000)
            except OKXError:
                continue
            if not res:
                continue                            # not in the exchange history yet: try again next run
            cache[key] = mirror.outcome(m, res) if a['action'] == 'closed_on_exchange' else {k: res[k] for k in ('pnl', 'price_pnl', 'funding', 'close_price', 'fee')}
        a.update(cache[key])


def dashboard_actions(out, actions):
    """Join confirmed execution-test fills with strategy actions without replaying orders."""
    rows, closed = list(actions), set()
    for path in sorted(out.glob('smoke-test-*.json')):
        report = json.loads(path.read_text())
        if report.get('demo') is not True:
            continue
        entry, exit_order = report.get('entry') or {}, report.get('exit') or {}
        if entry.get('state') != 'filled' or not entry.get('ordId'):
            continue
        for kind, fill in [('open', entry), ('close', exit_order)]:
            if fill.get('state') != 'filled' or not fill.get('ordId'):
                continue
            ts = datetime.fromtimestamp(int(fill['fillTime']) / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            action = dict(ts=ts, action=kind, inst=report['instrument'], strategy='execution_test', execution_test=True,
                          dry_run=False, side=1, contracts=fill['accFillSz'], price=float(fill['avgPx']),
                          leverage=int(fill['lever']), order_id=fill['ordId'])
            if kind == 'open':
                action['stop'] = report.get('stop')
            else:
                closed.add(entry['ordId'])
                action.update(reason='execution_test', pnl=float(fill.get('pnl') or 0),
                              fee=abs(float(entry.get('fee') or 0)) + abs(float(fill.get('fee') or 0)))
                action['net_pnl'] = action['pnl'] - action['fee']
            rows.append(action)
    unique = {}
    for a in rows:
        key = a.get('order_id') or (a.get('ts'), a.get('action'), a.get('inst'))
        unique[key] = a
    return sorted(unique.values(), key=lambda a: a['ts'], reverse=True)[:30], len(closed)


def decision_snapshot(data):
    """Publish only fixed signal fields, never source notes, chat or order identifiers."""
    root = Path(data); rows = []
    try:
        with (root/'combined/events.jsonl').open() as f: lines = deque(f,maxlen=2000)
    except OSError: lines = []
    reasons = {'same_side_risk','same_side_count','max_positions','daily_halt','already_at_mean','insufficient_margin','stop','target','timeout'}
    for line in lines:
        try:
            raw = json.loads(line)
            if not isinstance(raw,dict) or not isinstance(raw.get('ts'),str): continue
            if raw.get('type') not in {'signal','skip','daily_halt'}: continue
            stamp = datetime.fromisoformat(raw['ts'].replace('Z','+00:00'))
            if stamp.tzinfo is None: continue
            inst = raw.get('inst')
            if inst is not None and not re.fullmatch(r'[A-Z0-9]+-USDT-SWAP',inst): continue
            rows.append(dict(ts=stamp.isoformat(),type=raw['type'],inst=inst,
                             strategy=raw.get('strategy') if raw.get('strategy') in TAG else None,
                             side=raw.get('side') if raw.get('side') in (-1,1) else None,
                             reason=raw.get('reason') if raw.get('reason') in reasons else None))
        except (ValueError,KeyError,TypeError): continue
    try: updated = json.loads((root/'summary.json').read_text()).get('updated_at')
    except (OSError,ValueError): updated = None
    return dict(strategy_updated_at=updated,strategy_decisions=sorted(rows,key=lambda r:r['ts'],reverse=True)[:50])


def paper_dist(p, px=None):
    """Stop distance of a paper signal: 'dist' (published since 2026-11-21), else |entry − stop|, else |px − stop|."""
    d = p.get('dist')
    if d:
        return float(d)
    ref = p.get('entry') or px
    return abs(float(ref) - float(p['stop'])) if ref else 0.0


def valid_tp(x):
    """A take-profit price usable on OKX: a finite float > 0, else None (NEGATIVE TP RULE, execution EXIT RULES 5)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if 0 < x < float('inf') else None


def target_of(p, px):
    """(tp, skip_reason) for a new entry of signal p at the live price px (NEGATIVE TP RULE, 2026-11-25: 「负数止盈时不挂止盈，
    只靠止损和跟踪止盈出场」). tp None = open with NO take-profit (stop / trailing stop / paper exits only):
      - p['no_target'] (paper: target ≤ 0, or real_levels(): live target ≤ 0) → None, never the 3R fallback;
      - p['tp'] given: ≤ 0 / not a number / not beyond the paper entry on the signal's side → None (an older signals.json
        can carry a negative tp); a valid tp the live price has already reached → skip 'target_already_reached' (as before);
      - p['tp'] missing (signals from before 2026-11-21): px ± DEFAULT_TARGET_RR × the paper stop distance, None when that
        is ≤ 0; at or past px → 'target_already_reached' (as before)."""
    side = p['side']
    if p.get('no_target'):
        return None, None
    if p.get('tp') is not None:
        tp = valid_tp(p['tp'])
        # beyond the entry it was computed from: the paper entry, or for PRICE_OFF LIVE (real_levels) the live price
        entry = px if p.get('price_off_live') else valid_tp(p.get('entry'))
        if tp is None or (entry and (tp - entry) * side <= 0):
            return None, None
    else:
        tp = px + side * DEFAULT_TARGET_RR * paper_dist(p, px)
        if tp <= 0:
            return None, None
    if (tp - px) * side <= 0:
        return tp, 'target_already_reached'
    return tp, None


def price_off_live_fields(sig):
    """PRICE_OFF LIVE fields of a signals.json for the live follower: price_off_live (None when the document has no such
    dict: an older publisher; then nothing is opened or closed for it), price_off_live_invalid (coins whose entry is
    malformed: never opened, and their open position is never closed for missing from the list) and
    price_off_live_exits."""
    raw = sig.get('price_off_live')
    if not isinstance(raw, dict):
        return dict(price_off_live=None, price_off_live_invalid=set(), price_off_live_exits={})
    book, bad = {}, set()
    for inst, p in raw.items():
        try:
            if not (isinstance(inst, str) and re.fullmatch(r'[A-Z0-9]+-USDT-SWAP', inst) and isinstance(p, dict)):
                raise ValueError
            if p['side'] not in (1, -1) or int(p['opened']) <= 0 or not float(p['entry']) > 0 or not float(p['stop']) > 0 \
                    or int(p['tf']) not in PRICE_OFF_LIVE_BAR or not float(p['stop_atr']) > 0:
                raise ValueError
            book[inst] = p
        except (KeyError, TypeError, ValueError):
            bad.add(inst) if isinstance(inst, str) else None
    ex = sig.get('price_off_live_exits') if isinstance(sig.get('price_off_live_exits'), dict) else {}
    return dict(price_off_live=book, price_off_live_invalid=bad,
                price_off_live_exits={k: v for k, v in ex.items() if isinstance(k, str) and isinstance(v, str)})


ADOPT_ENV = 'OKX_ADOPT'


def adopt_config(env=None):
    """Manual positions the trader is told to adopt (OKX_ADOPT, a JSON list; unset / empty = none, the default).
    Each item: inst, posSide (long / short / net), stop (the floor: the exchange stop never goes looser than this),
    optional posId (adopt only that exact position), side (1 / -1, must agree with posSide), algoId (its resting stop) and
    r_dist (1R as a price distance for the trailing stop; missing or invalid = the stop is protected but never trailed).
    Invalid items are dropped with a message, so a typo never adopts anything."""
    raw = (os.environ if env is None else env).get(ADOPT_ENV, '').strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        print(f'{ADOPT_ENV} 不是合法 JSON，不接管任何仓位'); return []
    out = []
    for it in items if isinstance(items, list) else []:
        try:
            inst, ps = str(it['inst']), str(it.get('posSide') or 'net')
            stop = float(it['stop'])
            side = {'long': 1, 'short': -1}.get(ps) or int(it['side'])
            if not re.fullmatch(r'[A-Z0-9]+-USDT-SWAP', inst) or ps not in ('long', 'short', 'net') or side not in (1, -1) \
                    or int(it.get('side', side)) != side or not stop > 0:
                raise ValueError
            try:
                r = float(it.get('r_dist'))
                r = r if r > 0 else None
            except (TypeError, ValueError):
                r = None
            out.append(dict(inst=inst, posSide=ps, side=side, stop=stop, posId=str(it['posId']) if it.get('posId') else None,
                            algoId=str(it['algoId']) if it.get('algoId') else None, r_dist=r))
        except (KeyError, TypeError, ValueError):
            print(f'{ADOPT_ENV} 中有一项无效，已忽略')
    return out


def adopt_key(cfg):
    return f"adopt|{cfg['inst']}|{cfg.get('posId') or cfg['posSide']}"


class Mirror:
    def __init__(self, client, state, paper, now, dry_run=False, adopt=None):
        self.c, self.s, self.paper, self.now, self.dry = client, state, paper, now, dry_run
        # OKX_ADOPT (adopt_config): manual positions to adopt. [] = none, and earlier adopted records are released back to
        # manual; None (tools that do not read the setting, e.g. stop_demo) = keep managing what the state already holds.
        self.adopt = None if adopt is None else list(adopt)
        self.adopt_freed = set()          # coins whose adopted position was closed on a reverse signal in this run
        self.actions = []
        self.live = None                  # demo mode only: real_price, to skip entries while the demo price is off the real market
        self.follow_price_off = False     # live mode only (main): follow signals.json price_off_live (PRICE_OFF LIVE)
        self.new_order_risk = {}
        self.now_ms = int(now.timestamp() * 1000)
        self.specs = {}
        self._inst_caps = None

    def log(self, kind, inst, **kw):
        self.actions.append(dict(ts=self.now.isoformat(timespec='seconds').replace('+00:00', 'Z'), action=kind, inst=inst, dry_run=self.dry, **kw))

    def post(self, path, body):
        if path == '/api/v5/trade/order' and body.get('attachAlgoOrds') and not entries_allowed():
            return dict(code='RUNTIME_PAUSED', msg='New entries blocked by runtime stop latch', data=[])
        if self.dry:
            return {'code': '0', 'data': [{'sCode': '0', 'dry_run': True}]}
        return self.c.request('POST', path, body)

    # ---- exchange reads ------------------------------------------------------
    def load(self):
        cfg = self.c.request('GET', '/api/v5/account/config')
        if not ok(cfg):
            raise OKXError('account config: ' + first_error(cfg))
        c = cfg['data'][0]
        if str(c.get('acctLv')) == '1':
            raise OKXError('账户为现货模式，无法交易永续合约：请在 OKX 账户设置中切换到合约模式')
        if 'withdraw' in str(c.get('perm', '')):
            raise OKXError('密钥开启了提现权限，已拒绝运行')
        self.hedged = c.get('posMode') == 'long_short_mode'
        bal = self.c.request('GET', '/api/v5/account/balance?ccy=USDT')
        usdt = next((x for x in (bal.get('data') or [{}])[0].get('details', []) if x.get('ccy') == 'USDT'), {}) if ok(bal) else {}
        self.equity = float(usdt.get('eq') or 0)
        self.available = float(usdt.get('availBal') or 0)
        pos = self.c.request('GET', '/api/v5/account/positions?instType=SWAP')
        self.positions, self.all_positions = {}, {}
        managed = set(INSTRUMENTS) | {m['inst'] for m in self.s['mirrored'].values() if m.get('status') == 'open'}
        if getattr(self, 'adopt', None):
            managed |= {a['inst'] for a in self.adopt}
        for p in (pos.get('data') or []) if ok(pos) else []:
            if float(p.get('pos') or 0) == 0:
                continue
            key = (p.get('instId'), p.get('posSide', 'net'))
            # Every USDT-margined swap is shown on the dashboard (manual ones marked as not ours);
            # only managed instruments are ever traded, amended or closed by this program.
            if str(p.get('instId', '')).endswith('-USDT-SWAP'):
                self.all_positions[key] = p
            if p.get('instId') in managed:
                self.positions[key] = p
        algos = self.c.request('GET', '/api/v5/trade/orders-algo-pending?instType=SWAP&ordType=conditional')
        oco = self.c.request('GET', '/api/v5/trade/orders-algo-pending?instType=SWAP&ordType=oco')
        self.algos = [a for r in (algos, oco) if ok(r) for a in r.get('data') or [] if a.get('instId') in managed]
        self.specs = {}
        self._inst_caps = None
        self.load_instrument_caps()

    def spec(self, inst):
        if inst not in self.specs:
            r = self.c.request('GET', f'/api/v5/public/instruments?instType=SWAP&instId={inst}')
            d = (r.get('data') or [{}])[0] if ok(r) else {}
            try:
                max_lever = int(float(d.get('lever') or 0))
            except (TypeError, ValueError):
                max_lever = 0
            self.specs[inst] = dict(ctVal=float(d.get('ctVal') or 0), lotSz=d.get('lotSz') or '1', minSz=float(d.get('minSz') or 0), tickSz=d.get('tickSz') or '0.0001',
                                    maxLever=max_lever)
        return self.specs[inst]

    def load_instrument_caps(self):
        """Cache each SWAP's OKX max leverage for this run from the public instruments list."""
        if self._inst_caps is not None:
            return self._inst_caps
        caps = {}
        try:
            r = self.c.request('GET', '/api/v5/public/instruments?instType=SWAP')
            if ok(r):
                for d in r.get('data') or []:
                    inst = d.get('instId')
                    try:
                        lev = int(float(d.get('lever') or 0))
                    except (TypeError, ValueError):
                        lev = 0
                    if inst and lev >= 1:
                        caps[inst] = lev
        except (OKXError, TypeError, ValueError, KeyError):
            caps = {}
        self._inst_caps = caps
        return caps

    def inst_max_lever(self, inst):
        """Per-instrument OKX max leverage, or None if the public spec is missing or unusable (open() then refuses the
        new entry; there is no default cap to fall back to)."""
        caps = self.load_instrument_caps()
        if inst in caps:
            return caps[inst]
        try:
            n = int(self.spec(inst).get('maxLever') or 0)
        except (OKXError, TypeError, ValueError, KeyError):
            return None
        return n if n >= 1 else None

    def price(self, inst):
        r = self.c.request('GET', f'/api/v5/market/ticker?instId={inst}')
        return float((r.get('data') or [{}])[0].get('last') or 0) if ok(r) else 0.0

    def late_ok(self, inst, p):
        """An entry older than OPEN_WINDOW is still worth following when the price has not moved far from the paper
        entry: the distance left to the stop is 50–150 % of the paper stop distance (open() sizes by the distance left,
        so the risk in USDT is the same as for a fresh entry)."""
        px, entry, stop = self.price(inst), float(p.get('entry') or 0), float(p.get('stop') or 0)
        if not px or not entry or not stop or entry == stop:
            return False
        left = (px - stop) * p['side'] / abs(entry - stop)
        return LATE_BAND[0] <= left <= LATE_BAND[1]

    def mgn_mode(self, m):
        """Margin mode of a mirrored position: what the exchange reports, else what it was opened with. Records from
        before the field was stored were opened isolated (the mode before cross); new entries are cross (MARGIN_MODE)."""
        held = self.held(m['inst'], m.get('side'))
        return (held or {}).get('mgnMode') or m.get('mgnMode') or 'isolated'

    def held(self, inst, side):
        ps = 'long' if side == 1 else 'short'
        return self.positions.get((inst, ps)) or self.positions.get((inst, 'net'))

    # ---- actions ---------------------------------------------------------------
    def open(self, key, inst, p):
        if not entries_allowed():
            return self.log('skip', inst, reason='runtime_paused')
        px = self.price(inst)
        side = p['side']
        if not px:
            return self.log('skip', inst, reason='no_price')
        if self.live:                          # the paper signal was made on real prices: do not trade a demo price that is somewhere else
            real = self.live(inst)
            if real and abs(px / real - 1) > PRICE_TOLERANCE:
                return self.log('skip', inst, reason='demo_price_off', price=px, real_price=real)
        if p.get('price_off_live'):            # PRICE_OFF LIVE: stop / take-profit from real candles at this live price
            p = self.real_levels(inst, p, px)
            if p is None:
                return
        dist = (px - p['stop']) / px * side
        if dist < MIN_STOP_DIST:
            return self.log('skip', inst, reason='stop_too_close_or_crossed', price=px, stop=p['stop'])
        tp, why = target_of(p, px)             # tp None: no take-profit (NEGATIVE TP RULE), stop / trailing stop only
        if why:
            return self.log('skip', inst, reason=why, price=px, target=tp)
        sp = self.spec(inst)
        if not sp['ctVal']:
            return self.log('skip', inst, reason='no_instrument_spec')
        if tp is not None and quantize(tp, sp['tickSz'], ROUND_HALF_UP) <= 0:
            tp = None                          # a target that rounds to 0 on the tick is no valid TP either
        inst_cap = self.inst_max_lever(inst)
        if not inst_cap:                       # fail closed: without OKX's own leverage cap for this coin, no new entry
            return self.log('skip', inst, reason='no_leverage_spec')
        r = RISK
        evolved = bool(ACTIVE_PROFILE and ACTIVE_PROFILE.get('mode') == 'evolved')
        leader = evolved and bool(p.get('leader'))
        if evolved and p.get('risk') is not None:
            r = min(RISK_RANGE[1], max(RISK_RANGE[0], float(p['risk'])))
        elif evolved:
            r = RISK_RANGE[0]
        if evolved:                                # many positions on one side are one bet: the next one risks less
            crowd = sum(1 for m in self.s['mirrored'].values() if m.get('status') == 'open' and m.get('side') == side and not m.get('adopted'))
            if same_side_full(crowd, MAX_SAME_SIDE):   # ... and with MAX_SAME_SIDE already open on this side, none is added
                return self.log('skip', inst, reason='same_side_count', open_same_side=crowd, max_same_side=MAX_SAME_SIDE)
            r *= next((f for n, f in CROWD_STEPS if crowd >= n), 1.0)
            if inst in MAJORS:
                r *= MAJOR_FACTOR
            r = min(r, MAX_TRADE_RISK)             # major doubling and crowding still never exceed 10 %
        # TRAIL & RISK RULES b, c (execution.entry_risk_factor, decided by the paper account): same-champion same-bar share
        # and new / winding seat half risk. Missing (older signals.json) = 1. May take the risk below 5 % (intended).
        # PRICE_OFF LIVE: a follower that also opens price_off_live (live) uses the paper signal's live_risk_factor when
        # present: the seat's same-bar group counted with its price_off entries (1 paper + 1 price_off = ½ + ½); demo /
        # a follower ignoring price_off_live opens only the paper entry and keeps risk_factor.
        factor = 1.0
        rf = p.get('live_risk_factor') if getattr(self, 'follow_price_off', False) and p.get('live_risk_factor') is not None else p.get('risk_factor')
        if evolved and rf is not None:
            try:
                factor = min(1.0, max(0.0, float(rf)))
            except (TypeError, ValueError):
                factor = 1.0
        r *= factor
        if r <= 0:
            return self.log('skip', inst, reason='zero_risk_factor')
        risk = r * self.equity
        same_risk = 0.0
        for existing_key, m in (self.s['mirrored'].items() if SAME_SIDE_CAP is not None else ()):   # only needed for a same-side risk cap
            if m.get('status') != 'open' or m.get('side') != side or m.get('adopted'):
                continue
            if existing_key in self.new_order_risk:
                same_risk += self.new_order_risk[existing_key]
                continue
            held = self.held(m['inst'], side)
            if not held:
                continue
            avg = float(held.get('avgPx') or m.get('entry') or 0)
            if avg <= 0 or not m.get('stop'):
                return self.log('skip', inst, reason='existing_risk_unknown')
            same_risk += max((avg - float(m['stop'])) * side, 0) * abs(float(held['pos'])) * self.spec(m['inst'])['ctVal']
        if SAME_SIDE_CAP is not None and same_risk + risk > SAME_SIDE_CAP * self.equity * (1 + 1e-9):   # None = evolved mode: no same-side risk cap
            return self.log('skip', inst, reason='same_side_risk')
        contracts = quantize(risk / (px * dist) / sp['ctVal'], sp['lotSz'])
        if float(contracts) < sp['minSz'] or contracts <= 0:
            return self.log('skip', inst, reason='size_below_minimum', contracts=fmt(contracts), min=sp['minSz'])
        if evolved:
            try:
                want = int(p.get('leverage') or DEFAULT_LEVERAGE)
            except (TypeError, ValueError):
                want = DEFAULT_LEVERAGE
            want = max(1, min(want, EVOLVED_MAX_LEVERAGE))
            # per-instrument OKX cap (leverage only sets initial margin on cross)
            lever = clamp_leverage(want, cap=EVOLVED_MAX_LEVERAGE)
            lever = max(1, min(lever, inst_cap))
        else:
            cap = MAX_LEVERAGE
            try:
                want = int(p.get('leverage') or cap)
            except (TypeError, ValueError):
                want = cap
            lever = max(1, min(cap, want, int(.8 / dist), inst_cap))
        margin = float(contracts) * sp['ctVal'] * px / lever
        if margin > self.available * .95:
            return self.log('skip', inst, reason='insufficient_margin', margin=round(margin, 4), available=round(self.available, 4))
        # Cross-margin stop rule (全仓): the new stop, and every mirrored stop hit at once, must leave account equity
        # strictly above the remaining maintenance margin. Mirrored positions (including the ones opened earlier in this
        # run) are stopped in the worst case; manual positions on the account are never touched, they stay at the mark
        # and keep their maintenance margin. Leverage is not lowered to pass — the entry is skipped.
        book, unmanaged, prices, upnl = {}, {}, {}, 0.0
        mirrored_keys = set()
        for existing_key, m in self.s['mirrored'].items():
            if m.get('status') != 'open' or m.get('adopted'):   # adopted: counted below with the manual ones, as before
                continue
            held = self.held(m['inst'], m.get('side'))
            if held:
                qty_e = abs(float(held['pos'])) * self.spec(m['inst'])['ctVal']
                entry_e = float(held.get('avgPx') or m.get('entry') or 0)
                mark_e = float(held.get('markPx') or 0)
                mirrored_keys.add((held.get('instId'), held.get('posSide', 'net')))
            elif existing_key in self.new_order_risk:          # opened earlier in this run, not in the loaded positions
                qty_e = float(m.get('contracts') or 0) * self.spec(m['inst'])['ctVal']
                entry_e, mark_e = float(m.get('entry') or 0), 0.0
            else:
                continue
            if entry_e <= 0 or not m.get('stop'):
                return self.log('skip', inst, reason='existing_risk_unknown')
            px_e = mark_e or self.price(m['inst']) or entry_e      # OKX liquidates on the mark price
            book[m['inst']] = dict(side=m['side'], entry=entry_e, qty=qty_e, stop=float(m['stop']), fee=0.0, funding=0.0,
                                   mmr=maintenance_rate(m['inst']))
            prices[m['inst']] = px_e
            upnl += qty_e * (px_e - entry_e) * m['side']
        for k, hp in (getattr(self, 'all_positions', None) or {}).items():
            if k in mirrored_keys:
                continue
            try:
                n, entry_e = float(hp.get('pos') or 0), float(hp.get('avgPx') or 0)
                mark = float(hp.get('markPx') or hp.get('last') or 0) or entry_e
                ct = self.spec(k[0])['ctVal']
            except (TypeError, ValueError, KeyError):
                return self.log('skip', inst, reason='existing_risk_unknown')
            if not n or entry_e <= 0 or not ct:
                continue
            side_e = (1 if n > 0 else -1) if k[1] == 'net' else (1 if k[1] == 'long' else -1)
            key_e = f'{k[0]}#{k[1]}#manual'
            unmanaged[key_e] = dict(side=side_e, entry=entry_e, qty=abs(n) * ct, mmr=maintenance_rate(k[0]))
            prices[key_e] = mark
            upnl += abs(n) * ct * (mark - entry_e) * side_e
        qty_new = float(contracts) * sp['ctVal']
        new_p = dict(side=side, entry=px, qty=qty_new, stop=p['stop'], fee=qty_new * px * FEE, funding=0.0, mmr=maintenance_rate(inst))
        allowed, why = cross_open_allowed(self.equity - upnl, book, inst, new_p, prices, fixed=unmanaged)
        if not allowed:
            return self.log('skip', inst, reason=why or 'stop_before_liquidation', leverage=lever, stop_frac=round(dist, 6))
        pos_side = ('long' if side == 1 else 'short') if self.hedged else None
        lev_body = dict(instId=inst, lever=str(lever), mgnMode=MARGIN_MODE, **({'posSide': pos_side} if pos_side and MARGIN_MODE == 'isolated' else {}))
        r = self.post('/api/v5/account/set-leverage', lev_body)
        if not ok(r):
            return self.log('error', inst, step='set_leverage', detail=first_error(r))
        tick = sp['tickSz']
        algo = dict(slTriggerPx=fmt(quantize(p['stop'], tick, ROUND_HALF_UP)), slOrdPx='-1', slTriggerPxType='last',
                    **(dict(tpTriggerPx=fmt(quantize(tp, tick, ROUND_HALF_UP)), tpOrdPx='-1', tpTriggerPxType='last') if tp is not None else {}))
        cl = f"{TAG.get(p.get('strategy'), 'px')}{inst.split('-')[0].lower()}{int(p['opened']) // 1000}"[:32]
        order = dict(instId=inst, tdMode=MARGIN_MODE, side='buy' if side == 1 else 'sell', ordType='market', sz=fmt(contracts),
                     clOrdId=cl, attachAlgoOrds=[algo], **({'posSide': pos_side} if pos_side else {}))
        r = self.post('/api/v5/trade/order', order)
        if not ok(r):
            return self.log('error', inst, step='order', detail=first_error(r), size=fmt(contracts))
        self.s['mirrored'][key] = dict(inst=inst, side=side, posSide=pos_side or 'net', strategy=p.get('strategy'), contracts=fmt(contracts),
                                       stop=p['stop'], target=tp, leverage=lever, entry=px, init_stop=p['stop'], dist=paper_dist(p, px), best=px, clOrdId=cl, opened_at=self.now_ms, status='open', leader=leader, mgnMode=MARGIN_MODE, champion=p.get('champion'),
                                       live_dist=abs(px - float(p['stop'])), risk_factor=factor,
                                       ordId=(r.get('data') or [{}])[0].get('ordId'), **({'no_target': True} if tp is None else {}),
                                       **({'price_off_live': True, 'sim_stop': p.get('sim_stop'), 'sim_tp': p.get('sim_tp'),
                                           'real_atr': p.get('real_atr'), 'atr_bar': p.get('atr_bar')} if p.get('price_off_live') else {}))
        self.new_order_risk[key] = float(contracts) * sp['ctVal'] * abs(px - p['stop'])
        self.available -= margin
        self.log('open', inst, side=side, strategy=p.get('strategy'), contracts=fmt(contracts), price=px, stop=algo['slTriggerPx'],
                 target=algo.get('tpTriggerPx'), leverage=lever, risk_usdt=round(risk, 2), **({'leader': True} if leader else {}),
                 **({'no_target': True} if tp is None else {}),
                 **({'price_off_live': True, 'sim_stop': p.get('sim_stop'), 'sim_tp': p.get('sim_tp')} if p.get('price_off_live') else {}),
                 **({'risk_factor': round(factor, 4)} if factor != 1.0 else {}))

    def real_levels(self, inst, p, px):
        """PRICE_OFF LIVE: the signal's stop / take-profit recomputed from the REAL market (never the paper's or the demo's
        prices): ATR(14) of the genome's timeframe (execution.Series.atr, the same formula as execution.stop_distance) on
        OKX's closed real candles up to the signal bar (the bar that closes at the fill time `opened`), then
        stop = px ∓ stop_atr × ATR and tp = px ± target_rr × that distance at the live price px (target_rr mapped by
        execution.target_rr: a gene < .5 is DEFAULT_TARGET_RR). Only the live follower calls this (its client reads the
        real market); None (logged skip) when the real candles are not usable, or when the recomputed STOP is not a positive
        price (stop_not_positive: every position must have a valid stop). A recomputed take-profit that is not a valid price
        (≤ 0: a short whose target_rr × distance exceeds the price) opens WITHOUT a take-profit (tp None, no_target True;
        NEGATIVE TP RULE 2026-11-25). The live decision replaces the sim's: a sim no_target is dropped when the live tp is
        valid, and set when it is not."""
        if not getattr(self, 'follow_price_off', False):
            self.log('skip', inst, reason='price_off_live_not_live'); return None
        try:
            tf, k, side, opened = int(p['tf']), float(p['stop_atr']), int(p['side']), int(p['opened'])
        except (KeyError, TypeError, ValueError):
            self.log('skip', inst, reason='price_off_live_bad_signal'); return None
        bar = PRICE_OFF_LIVE_BAR.get(tf)
        if not bar or not k > 0:
            self.log('skip', inst, reason='price_off_live_bad_signal'); return None
        try:
            r = self.c.request('GET', f'/api/v5/market/candles?instId={inst}&bar={bar}&limit=300')
        except OKXError:
            r = {}
        rows = {}
        for row in (r.get('data') or []) if ok(r) else []:
            try:
                ts = int(row[0])
                if (len(row) > 8 and str(row[8]) != '1') or ts + tf * 3600_000 > opened:
                    continue                                   # open candles and bars after the signal bar are not used
                rows[ts] = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
            except (TypeError, ValueError, IndexError):
                continue
        ts = sorted(rows)
        if len(ts) < PRICE_OFF_LIVE_MIN_BARS or ts[-1] + tf * 3600_000 != opened:
            self.log('skip', inst, reason='real_atr_unavailable', bars=len(ts)); return None
        x = Series(inst, ts, *[[rows[t][j] for t in ts] for j in range(4)])
        atr = x.atr()[-1]
        dist = k * atr
        if not dist > 0:
            self.log('skip', inst, reason='real_atr_unavailable', bars=len(ts)); return None
        rr = target_rr({'target_rr': p.get('target_rr')})
        stop, tp = px - side * dist, take_profit(px, side, rr, dist)
        if stop <= 0:                              # a long whose distance is more than the price: no valid stop, no entry
            self.log('skip', inst, reason='stop_not_positive', price=px, stop=stop); return None
        out = dict({k: v for k, v in p.items() if k != 'no_target'}, stop=stop, tp=tp, dist=dist, sim_stop=p.get('stop'),
                   sim_tp=p.get('tp'), real_atr=atr, atr_bar=ts[-1])
        if tp is None:                             # a short whose target_rr × distance is more than the price: no TP
            out['no_target'] = True
        return out

    def fill_result(self, inst, pos_side='net', since_ms=0, until_ms=None):
        """Realised result of the latest close of `inst` from OKX position history (read only), or None."""
        r = self.c.request('GET', f'/api/v5/account/positions-history?instType=SWAP&instId={inst}&limit=20')
        rows = [x for x in (r.get('data') or []) if ok(r) and x.get('instId') == inst
                and (pos_side in ('net', None) or x.get('posSide', 'net') in (pos_side, 'net'))
                and int(x.get('uTime') or 0) >= since_ms and (until_ms is None or int(x.get('uTime') or 0) <= until_ms)]
        if not rows:
            return None
        x = max(rows, key=lambda x: int(x.get('uTime') or 0))
        num = lambda k: float(x[k]) if x.get(k) not in (None, '') else None
        return dict(pnl=num('realizedPnl'), price_pnl=num('pnl'), funding=num('fundingFee'), close_price=num('closeAvgPx'), open_price=num('openAvgPx'), fee=num('fee'),
                    closed_at=int(x.get('uTime') or 0), liquidated=str(x.get('type')) in ('3', '4'))

    def outcome(self, m, res):
        """Which exchange order closed it: the stop, the target, or a liquidation. A position without a take-profit
        (no_target / target None) can only have been closed by its stop (or trailing stop), even in profit."""
        if not res:
            return {}
        px, stop, target = res.get('close_price'), m.get('stop'), m.get('target')
        kind = 'liquidation' if res.get('liquidated') else None
        if not kind and (m.get('no_target') or target is None):
            kind = 'stop'
        elif not kind and px and stop and target:
            kind = 'stop' if abs(px - float(stop)) <= abs(px - float(target)) else 'target'
        elif not kind and res.get('pnl') is not None:
            kind = 'target' if res['pnl'] >= 0 else 'stop'
        return dict({k: v for k, v in res.items() if k in ('pnl', 'price_pnl', 'funding', 'close_price', 'fee')}, outcome=kind)

    def close(self, key, m, reason, **extra_log):
        body = dict(instId=m['inst'], mgnMode=self.mgn_mode(m), autoCxl=True, **({'posSide': m['posSide']} if m['posSide'] != 'net' else {}))
        r = self.post('/api/v5/trade/close-position', body)
        if not ok(r):
            return self.log('error', m['inst'], step='close', detail=first_error(r))
        m['status'] = 'done'; m['closed_reason'] = reason
        for k in [k for k in self.positions if k[0] == m['inst'] and k[1] in (m['posSide'], 'net')]:
            self.positions.pop(k)  # gone now, so a fresh paper entry on this coin can be mirrored in the same run
        try:  # the close fills at market; its history row is usually there at once, otherwise the summary fills it in later
            res = self.fill_result(m['inst'], m['posSide'], int(m.get('opened_at') or 0))
        except OKXError:
            res = None
        extra = {k: res[k] for k in ('pnl', 'price_pnl', 'funding', 'close_price', 'fee')} if res else {}
        self.book_leader(m, res)
        self.log('close', m['inst'], strategy=m.get('strategy'), reason=reason, **extra, **extra_log)

    def ensure_stop(self, key, m, p):
        """Every open position must have its stop resting on the exchange. If none is found for a position opened in an
        earlier run (the attached stop was cancelled or never registered), place it again at the paper stop; if the price
        is already beyond that stop, close the position."""
        if int(m.get('opened_at') or 0) >= self.now_ms:
            return True                                            # opened in this run: its stop is not in this run's snapshot yet
        if any(a['instId'] == m['inst'] and (a.get('posSide', 'net') == m['posSide'] or m['posSide'] == 'net') and a.get('slTriggerPx')
               for a in self.algos):
            return True
        stop, px = float(p['stop'] if p else m['stop']), self.price(m['inst'])
        if px and (px - stop) * m['side'] <= 0:
            self.close(key, m, 'stop_missing_price_beyond_stop')
            return False
        tick = self.spec(m['inst'])['tickSz']
        body = dict(instId=m['inst'], tdMode=self.mgn_mode(m), side='sell' if m['side'] == 1 else 'buy', ordType='conditional', closeFraction='1',
                    slTriggerPx=fmt(quantize(stop, tick, ROUND_HALF_UP)), slOrdPx='-1', slTriggerPxType='last',
                    **({'posSide': m['posSide']} if m['posSide'] != 'net' else {'reduceOnly': True}))
        r = self.post('/api/v5/trade/order-algo', body)
        if not ok(r):
            self.log('error', m['inst'], step='restore_stop', detail=first_error(r))
        else:
            self.log('restore_stop', m['inst'], stop=body['slTriggerPx']); m['stop'] = stop
        return True

    def mirror_algos(self, m, kind):
        """This mirror's resting algos that carry a stop (kind 'sl') or a take-profit (kind 'tp')."""
        return [a for a in self.algos if a['instId'] == m['inst'] and (a.get('posSide', 'net') == m['posSide'] or m['posSide'] == 'net')
                and a.get(f'{kind}TriggerPx') not in (None, '', '0')]

    def ensure_target(self, key, m, p):
        """Every mirrored position must have a take-profit resting on the exchange, as well as its stop. Target = the paper
        target (signals 'tp'), else the one stored at entry, else entry ± DEFAULT_TARGET_RR × the stop distance. Missing:
        place a reduce-only conditional TP order (or close at market when the price is already past it, take_profit);
        resting but away from the paper target by more than AMEND_TOLERANCE: amend it. Returns False when it closed.
        NO-TARGET positions (NEGATIVE TP RULE: m / p no_target) are left alone: nothing placed, amended or closed here.
        A target ≤ 0 (paper tp of an older signals.json, stored, or the 3R fallback) is never placed or amended: the next
        valid candidate is used, and when every known one is ≤ 0 the mirror becomes no_target."""
        if m.get('no_target') or (p and p.get('no_target')):
            return True                                            # no take-profit by rule: stop / trailing stop only
        if int(m.get('opened_at') or 0) >= self.now_ms:
            return True                                            # opened in this run with its TP attached
        dist = self.mirror_dist(m, p)
        entry = float(m.get('entry') or 0)
        fallback = entry + m['side'] * DEFAULT_TARGET_RR * dist if entry and dist else None
        paper_tp = valid_tp((p or {}).get('tp'))
        tp = paper_tp or valid_tp(m.get('target')) or valid_tp(fallback)
        if not tp:
            if fallback is not None or any((x or {}).get(k) is not None for x, k in ((p, 'tp'), (m, 'target'))):
                m['no_target'], m['target'] = True, None           # every known target is ≤ 0: no TP, never a negative one
                return True
            self.log('error', m['inst'], step='ensure_target', detail='target unknown')
            return True
        tick = self.spec(m['inst'])['tickSz']
        resting = self.mirror_algos(m, 'tp')
        if resting:
            cur = float(resting[0]['tpTriggerPx'])
            if paper_tp and abs(tp - cur) / cur > AMEND_TOLERANCE:
                r = self.post('/api/v5/trade/amend-algos', dict(instId=m['inst'], algoId=resting[0]['algoId'],
                                                                 newTpTriggerPx=fmt(quantize(tp, tick, ROUND_HALF_UP))))
                if not ok(r):
                    self.log('error', m['inst'], step='amend_target', detail=first_error(r))
                else:
                    self.log('amend_target', m['inst'], old=cur, new=tp); m['target'] = tp
            else:
                m.setdefault('target', cur)
            return True
        px = self.price(m['inst'])
        if px and (px - tp) * m['side'] >= 0:
            self.close(key, m, 'take_profit')
            return False
        held = self.held(m['inst'], m['side'])
        body = dict(instId=m['inst'], tdMode=self.mgn_mode(m), side='sell' if m['side'] == 1 else 'buy', ordType='conditional',
                    sz=fmt(Decimal(str(abs(float((held or {}).get('pos') or m.get('contracts') or 0))))),
                    tpTriggerPx=fmt(quantize(tp, tick, ROUND_HALF_UP)), tpOrdPx='-1', tpTriggerPxType='last',
                    **({'posSide': m['posSide']} if m['posSide'] != 'net' else {'reduceOnly': True}))
        r = self.post('/api/v5/trade/order-algo', body)
        if not ok(r):
            self.log('error', m['inst'], step='restore_target', detail=first_error(r))
        else:
            self.log('restore_target', m['inst'], target=body['tpTriggerPx']); m['target'] = tp
        return True

    def mirror_dist(self, m, p):
        """The paper stop distance of this position (|entry − initial stop|): signals 'dist', else the one stored at entry,
        else |entry − initial stop| of the mirror (records from before 2026-11-21 only have the current stop)."""
        d = (p or {}).get('dist') or m.get('dist')
        if not d:
            entry, stop = float(m.get('entry') or 0), float(m.get('init_stop') or m.get('stop') or 0)
            # only a stop still on the losing side of the entry is a usable distance (a stop already trailed past the
            # entry is not); otherwise wait for signals.json to publish 'dist' (next paper run, ≤ 4 h)
            d = (entry - stop) * m['side'] if entry and stop and (entry - stop) * m['side'] > 0 else 0
        if d:
            m['dist'] = float(d)
        return float(d or 0)

    def best_price(self, m):
        """Best price since the entry, from OKX's CLOSED 1H candles that started after the mirror opened (the same bars the
        paper trail uses) and the best already stored. Returns the stored best when the candles cannot be read."""
        best = float(m.get('best') or m.get('entry') or 0)
        opened = int(m.get('opened_at') or 0)
        hours = max(1, (self.now_ms - opened) // 3600_000 + 2)
        try:
            r = self.c.request('GET', f"/api/v5/market/candles?instId={m['inst']}&bar=1H&limit={min(300, hours)}")
        except OKXError:
            return best
        for row in (r.get('data') or []) if ok(r) else []:
            try:
                if int(row[0]) < opened or (len(row) > 8 and str(row[8]) != '1'):
                    continue
                best = max(best, float(row[2])) if m['side'] == 1 else min(best, float(row[3]))
            except (TypeError, ValueError, IndexError):
                continue
        m['best'] = best
        return best

    def live_entry(self, m):
        """The live position's own entry: OKX avgPx when the position is loaded, else the price stored at entry."""
        held = self.held(m['inst'], m['side'])
        try:
            avg = float((held or {}).get('avgPx') or 0)
        except (TypeError, ValueError):
            avg = 0.0
        return avg or float(m.get('entry') or 0)

    def live_dist(self, m):
        """|live entry − live initial stop| (TRAIL & RISK RULES a), 0 when unknown. The initial stop is `init_stop` (stored
        at entry since 2026-11-21); `live_dist` is the distance stored at entry (ticker price − stop). State entries
        without either (older ones) return 0, and the trail falls back to the paper dist."""
        entry, stop0 = self.live_entry(m), float(m.get('init_stop') or 0)
        if entry and stop0 and (entry - stop0) * m['side'] > 0:
            return (entry - stop0) * m['side']
        try:
            return float(m.get('live_dist') or 0)
        except (TypeError, ValueError):
            return 0.0

    def trail_stop(self, key, m, p):
        """Trailing stop / breakeven on the exchange (execution.trail_stop_level, once per run), with R = trail_unit(paper
        dist, live dist) = the smaller of the two and the live entry: once the best price since the entry is BREAKEVEN_R
        × R in profit the stop goes to the entry, once it is TRAIL_START_R × R in profit the stop is best ∓ TRAIL_DIST_R × R;
        the paper's own stop is used when it is tighter. The exchange stop only moves in the position's favour
        (amend-algos); if the price is already beyond the new stop, the position is closed at market (trailing_stop)."""
        if not self.mirror_algos(m, 'sl'):
            return                                                 # opened / stop restored in this run: trail from the next run
        paper_d, live_d = self.mirror_dist(m, p), self.live_dist(m)
        unit, entry = trail_unit(paper_d, live_d), self.live_entry(m)
        want, trailing = float(m['stop']), False
        if p and p.get('stop') and (float(p['stop']) - want) * m['side'] > 0:
            want, trailing = float(p['stop']), bool(p.get('trailing'))
        if unit and entry:
            best = self.best_price(m)
            t, on = trail_stop_level(m['side'], entry, best, want, unit, TRAIL)
            if on and (t - want) * m['side'] > 0:
                want, trailing = t, True
            m['trail_unit'] = unit
        if (want - m['stop']) * m['side'] <= 0 or abs(want - m['stop']) / m['stop'] <= AMEND_TOLERANCE:
            return
        if trailing:
            m['trailing'] = True
        px = self.price(m['inst'])
        if px and (px - want) * m['side'] <= 0:
            return self.close(key, m, 'trailing_stop' if trailing else 'stop_loss')
        self.amend_stop(key, m, want)

    def amend_stop(self, key, m, new_stop):
        algo = next(iter(self.mirror_algos(m, 'sl')), None)
        if not algo:
            return self.log('error', m['inst'], step='amend_stop', detail='algo order not found')
        tick = self.spec(m['inst'])['tickSz']
        r = self.post('/api/v5/trade/amend-algos', dict(instId=m['inst'], algoId=algo['algoId'], newSlTriggerPx=fmt(quantize(new_stop, tick, ROUND_HALF_UP))))
        if not ok(r):
            return self.log('error', m['inst'], step='amend_stop', detail=first_error(r))
        self.log('amend_stop', m['inst'], old=m['stop'], new=new_stop); m['stop'] = new_stop

    # ---- adopted manual positions (OKX_ADOPT) -----------------------------------------
    def adopted_held(self, m):
        """The live position of an adopted record: same instId + posSide, and the same posId when one is configured."""
        p = (getattr(self, 'all_positions', None) or self.positions).get((m['inst'], m['posSide']))
        if not p or (m.get('posId') and str(p.get('posId') or '') != m['posId']):
            return None
        n = float(p.get('pos') or 0)
        if m['posSide'] == 'net' and (n > 0) != (m['side'] == 1):
            return None
        return p

    def adopted_of(self, p):
        return any(m.get('adopted') and m['status'] == 'open' and m['inst'] == p.get('instId') and m['posSide'] == p.get('posSide', 'net')
                   for m in self.s['mirrored'].values())

    def adopted_algo(self, m):
        """The adopted position's resting stop: the configured / stored algoId first, else any stop on that position."""
        sl = self.mirror_algos(m, 'sl')
        return next((a for a in sl if m.get('algoId') and str(a.get('algoId')) == m['algoId']), None) or next(iter(sl), None)

    def release_adopted(self):
        """Rollback: an open adopted record whose entry was removed from OKX_ADOPT goes back to being a manual position
        (status 'released'; nothing is sent to the exchange, its stop stays as it is)."""
        keys = {adopt_key(c) for c in self.adopt}
        for key, m in self.s['mirrored'].items():
            if m.get('adopted') and m.get('status') == 'open' and key not in keys:
                m['status'] = 'released'; self.log('adopt_release', m['inst'], stop=m.get('stop'))

    def adopt_positions(self):
        """Adopt each configured manual position once: only when the live position with that instId + posSide (+ posId)
        exists. A record (done or open) is never adopted again; no other manual position is ever adopted."""
        pending = self.s.setdefault('adopt_pending', {})
        for cfg in getattr(self, 'adopt', ()):
            key = adopt_key(cfg)
            if key in self.s['mirrored'] and self.s['mirrored'][key].get('status') != 'released':
                continue                                       # open, or done (stop hit / closed): never adopted again
            p = self.adopted_held(dict(cfg, posId=cfg.get('posId')))
            if not p:
                why = 'position_not_found'
                alt = (getattr(self, 'all_positions', None) or {}).get((cfg['inst'], cfg['posSide']))
                if alt and cfg.get('posId') and str(alt.get('posId') or '') != cfg['posId']:
                    why = 'posId_mismatch'
                if pending.get(key) != why:                    # log a reason once, not every hour
                    pending[key] = why; self.log('skip', cfg['inst'], reason='adopt_' + why)
                continue
            pending.pop(key, None)
            stop = cfg['stop']
            m = dict(inst=cfg['inst'], side=cfg['side'], posSide=cfg['posSide'], posId=cfg.get('posId'), strategy='adopted', adopted=True,
                     contracts=str(abs(float(p.get('pos') or 0))), entry=float(p.get('avgPx') or 0), stop=stop, floor=stop, init_stop=stop,
                     target=None, leverage=p.get('lever'), mgnMode=p.get('mgnMode') or 'isolated', algoId=cfg.get('algoId'),
                     r_dist=cfg.get('r_dist'), best=float(p.get('avgPx') or 0), opened_at=self.now_ms, adopted_at=self.now_ms,
                     pos_ctime=int(p['cTime']) if str(p.get('cTime') or '').isdigit() else None, status='open', leader=False)
            algo = self.adopted_algo(m)
            if algo:
                m['algoId'] = str(algo.get('algoId'))
                cur = float(algo['slTriggerPx'])
                if (cur - stop) * m['side'] > 0:
                    m['stop'] = cur                            # the resting stop is already tighter than the floor: keep it
            self.s['mirrored'][key] = m
            self.log('adopt', m['inst'], side=m['side'], posSide=m['posSide'], contracts=m['contracts'], price=m['entry'], stop=m['stop'],
                     floor=stop, r_dist=m['r_dist'], mgnMode=m['mgnMode'])

    def manage_adopted(self, key, m, p):
        """One run for an adopted position, in this order: gone → done (never re-adopted); no resting stop → place one at
        the stored stop (never looser than the floor) or, only when the price is already past it, close
        (stop_missing_price_beyond_stop); a NEW paper signal the other way on the coin → close (reverse_signal); else the
        PR #110 trailing stop with R = the configured r_dist, amended on the existing algo, only ever tightened. Size,
        leverage and margin mode are never changed; no cancel endpoint is ever called."""
        held = self.adopted_held(m)
        if not held:
            m['status'] = 'done'
            try:
                res = self.fill_result(m['inst'], m['posSide'], int(m.get('opened_at') or 0)) or {}
            except OKXError:
                res = {}
            m['closed_reason'] = 'liquidation' if res.get('liquidated') else 'trailing_stop' if m.get('trailing') else 'stop_or_manual'
            if res.get('pnl') is not None:
                m['realized_pnl'] = res['pnl']
            extra = {k: res[k] for k in ('pnl', 'price_pnl', 'funding', 'close_price', 'fee') if k in res}
            return self.log('closed_on_exchange', m['inst'], strategy='adopted', adopted=True, reason=m['closed_reason'], **extra)
        floor, side = float(m.get('floor') or m['stop']), m['side']
        stop = max(float(m['stop']) * side, floor * side) * side       # never looser than the floor
        algo = self.adopted_algo(m)
        if not algo:
            px = self.price(m['inst'])
            if px and (px - stop) * side <= 0:
                return self.close(key, m, 'stop_missing_price_beyond_stop', adopted=True)
            tick = self.spec(m['inst'])['tickSz']
            body = dict(instId=m['inst'], tdMode=held.get('mgnMode') or m.get('mgnMode') or 'isolated', side='sell' if side == 1 else 'buy',
                        ordType='conditional', closeFraction='1', reduceOnly=True,
                        slTriggerPx=fmt(quantize(stop, tick, ROUND_UP if side == 1 else ROUND_DOWN)), slOrdPx='-1', slTriggerPxType='last',
                        **({'posSide': m['posSide']} if m['posSide'] != 'net' else {}))
            r = self.post('/api/v5/trade/order-algo', body)
            if not ok(r):
                return self.log('error', m['inst'], step='adopt_restore_stop', detail=first_error(r))
            m['stop'] = stop
            m['algoId'] = str((r.get('data') or [{}])[0].get('algoId') or '') or None
            return self.log('restore_stop', m['inst'], adopted=True, stop=body['slTriggerPx'])
        m['algoId'] = str(algo.get('algoId'))
        cur = float(algo['slTriggerPx'])
        if (cur - stop) * side > 0:
            stop = cur                                         # tightened on the exchange (by hand): never loosen it again
        m['stop'] = stop
        if p and p.get('side') == -side and int(p.get('opened') or 0) >= int(m.get('adopted_at') or 0):
            self.close(key, m, 'reverse_signal', adopted=True)
            if m['status'] == 'done':
                self.__dict__.setdefault('adopt_freed', set()).add(m['inst'])
            return
        unit = m.get('r_dist')
        try:
            unit = float(unit) if unit is not None and float(unit) > 0 else 0.0
        except (TypeError, ValueError):
            unit = 0.0
        if not unit:
            if not m.get('no_trail_logged'):
                m['no_trail_logged'] = True; self.log('skip', m['inst'], reason='adopt_no_r_dist_no_trail', stop=stop)
            want = stop
        else:
            entry = float(held.get('avgPx') or m.get('entry') or 0)
            want, trailing = stop, False
            if entry:
                t, on = trail_stop_level(side, entry, self.best_price(m), stop, unit, TRAIL)
                if on and (t - stop) * side > 0:
                    want, trailing = t, True
            if trailing:
                m['trailing'] = True
        below_floor = (floor - cur) * side > 0                # a resting stop looser than the floor is always raised to it
        if (want - cur) * side <= 0 or (abs(want - cur) / cur <= AMEND_TOLERANCE and not below_floor):
            return
        px = self.price(m['inst'])
        if px and (px - want) * side <= 0:
            return self.close(key, m, 'trailing_stop', adopted=True)
        tick = self.spec(m['inst'])['tickSz']
        new = fmt(quantize(want, tick, ROUND_DOWN if side == 1 else ROUND_UP))
        if (float(new) - floor) * side < 0:
            new = fmt(quantize(floor, tick, ROUND_UP if side == 1 else ROUND_DOWN))
        r = self.post('/api/v5/trade/amend-algos', dict(instId=m['inst'], algoId=m['algoId'], newSlTriggerPx=new))
        if not ok(r):
            return self.log('error', m['inst'], step='adopt_amend_stop', detail=first_error(r))   # the resting stop stays as it is
        self.log('amend_stop', m['inst'], adopted=True, old=cur, new=float(new)); m['stop'] = float(new)

    def leader_upl(self):
        """Floating PnL of the leader's open positions, and remember each one's so a later close can be booked."""
        total = 0.0
        for m in self.s['mirrored'].values():
            if m.get('status') == 'open' and m.get('leader'):
                held = self.held(m['inst'], m['side'])
                if held:
                    m['last_upl'] = float(held.get('upl') or 0); total += m['last_upl']
        return total

    def book_leader(self, m, res):
        """A leader position closed: its result leaves the daily-halt arithmetic (exchange result, else the last floating PnL)."""
        if m.get('leader'):
            pnl = (res or {}).get('pnl')
            self.s['leader_realized'] = float(self.s.get('leader_realized') or 0) + float(pnl if pnl is not None else m.get('last_upl') or 0)

    def flatten(self, reason, keep_leader=False, keep_adopted=False):
        for key, m in self.s['mirrored'].items():
            if keep_leader and m.get('leader'):
                continue
            if keep_adopted and m.get('adopted'):
                continue                       # the daily halt leaves the adopted position to its owner (its stop stays)
            if m['status'] == 'open' and self.held(m['inst'], m['side']):
                self.close(key, m, reason)
            elif m['status'] == 'open':
                m['status'] = 'done'

    # ---- one reconciliation pass ---------------------------------------------------
    def run(self, action='run'):
        self.load()
        if getattr(self, 'adopt', None) is not None:
            self.release_adopted()
        if getattr(self, 'adopt', None):
            self.adopt_positions()
        day = local_day(self.now)
        leader_upl = self.leader_upl()
        if self.s.get('day') != day:
            self.s.update(day=day, day_start=self.equity, halted=False, leader_realized=0.0, leader_upl_start=leader_upl)
        if action == 'pause':
            self.s['paused'] = True; self.log('pause', '-')
        elif action == 'resume':
            self.s['paused'] = False; self.log('resume', '-')
        elif action == 'follow-current':
            # Manual catch-up: entries skipped only for being older than OPEN_WINDOW are retried at the current price;
            # the stop / target / size / margin checks in open() still apply.
            for key, m in list(self.s['mirrored'].items()):
                if m['status'] == 'skipped' and m.get('reason') == 'too_old':
                    del self.s['mirrored'][key]
            self.log('follow_current', '-')
        elif action == 'flatten':
            self.s['paused'] = True; self.flatten('manual_flatten'); self.log('flatten', '-')
            return
        # Daily halt: today's change in equity for the whole account, including the leader. It closes and blocks everyone.
        day_pnl = self.equity - (self.s.get('day_start') or self.equity)
        if not self.s['halted'] and self.s.get('day_start') and day_pnl <= -self.s['day_start'] * DAILY_HALT:
            self.s['halted'] = True
            self.log('daily_halt', '-', equity=self.equity, day_start=self.s['day_start'], others_day=round(day_pnl, 2), leader_day=0.0)
            self.flatten('daily_halt', keep_leader=False, keep_adopted=True)
        paper_positions = self.paper.get('positions', {})
        # Closes and stop updates for what we mirrored.
        for key, m in self.s['mirrored'].items():
            if m['status'] != 'open':
                continue
            if m.get('adopted'):
                self.manage_adopted(key, m, paper_positions.get(m['inst']))
                continue
            held = self.held(m['inst'], m['side'])
            off = bool(m.get('price_off_live'))
            book = self.paper.get('price_off_live') if off else paper_positions
            p = (book or {}).get(m['inst'])
            same = p and f"{m['inst']}|{int(p['opened'])}" == key
            # PRICE_OFF LIVE: a signals.json without the list (None), or whose entry for this coin was not valid, says
            # nothing about this position: keep managing it on its own exchange orders, never close it for that.
            unknown = off and (book is None or m['inst'] in (self.paper.get('price_off_live_invalid') or ()))
            if off:
                p = {k: v for k, v in p.items() if k not in ('stop', 'tp', 'dist', 'trailing', 'no_target')} if same else None   # live's own levels only
            if not held:
                m['status'] = 'done'
                try:
                    res = self.outcome(m, self.fill_result(m['inst'], m['posSide'], int(m.get('opened_at') or 0)))
                except OKXError:
                    res = {}
                why = EXCHANGE_EXIT_REASONS.get(res.get('outcome'), 'exchange_stop_or_target')
                m['closed_reason'] = 'trailing_stop' if why == 'stop_loss' and m.get('trailing') else why
                if res.get('pnl') is not None:
                    m['realized_pnl'] = res['pnl']
                self.book_leader(m, res)
                self.log('closed_on_exchange', m['inst'], strategy=m.get('strategy'), reason=m['closed_reason'], **res)
            elif not same and not unknown:
                paper_why = (self.paper.get('price_off_live_exits' if off else 'exits') or {}).get(key)
                self.close(key, m, PAPER_EXIT_REASONS.get(paper_why, 'paper_closed'), **({'paper_reason': paper_why} if paper_why else {}),
                           **({'price_off_live': True} if off else {}))
            elif not self.ensure_stop(key, m, p):
                continue
            elif self.ensure_target(key, m, p):
                self.trail_stop(key, m, p)
        # New entries (a dry run previews them as if resumed).
        if self.s['paused'] and not self.dry:
            return
        # Priority when the same-side cap fills up: the higher-ranked team member first (rank 1 = leader), then the coin list order.
        order = lambda kv: (kv[1].get('rank', 99), INSTRUMENTS.index(kv[0]) if kv[0] in INSTRUMENTS else 99, kv[0])
        for inst, p in sorted(paper_positions.items(), key=order):
            key = f"{inst}|{int(p['opened'])}"
            if key in self.s['mirrored'] or inst not in INSTRUMENTS:
                continue
            if self.s['halted']:                               # halted for the day: no new entries, including the leader
                continue
            if inst in getattr(self, 'adopt_freed', ()):                       # adopted position closed on this reverse signal: the entry waits a run
                self.log('skip', inst, reason='adopted_closed_this_run')
                continue
            if self.held(inst, p['side']) or any(k[0] == inst for k in self.positions):
                self.log('skip', inst, reason='exchange_position_exists_not_ours'); self.s['mirrored'][key] = dict(inst=inst, status='skipped', side=p['side'], posSide='net')
                continue
            if self.now_ms - int(p['opened']) > OPEN_WINDOW and action != 'follow-current' and not self.late_ok(inst, p):
                self.s['mirrored'][key] = dict(inst=inst, status='skipped', side=p['side'], posSide='net', reason='too_old')
                self.log('skip', inst, reason='paper_entry_too_old')
                continue
            self.open(key, inst, p)
        if getattr(self, 'follow_price_off', False):
            self.follow_price_off_entries(action, order)

    def follow_price_off_entries(self, action, order):
        """PRICE_OFF LIVE entries (live only), after the paper ones, with every gate of the paper entries above (dedupe by
        inst|opened, scan list, daily halt, a position on the coin that is not ours, OPEN_WINDOW / late_ok) and all of
        open() (size, leverage, margin, same-side count, cross stop rule); open() recomputes stop / take-profit from real
        candles. A coin already held by one of our own mirrors is skipped (one position per coin)."""
        for inst, p in sorted((self.paper.get('price_off_live') or {}).items(), key=order):
            p = dict(p, price_off_live=True)
            key = f"{inst}|{int(p['opened'])}"
            if key in self.s['mirrored'] or inst not in INSTRUMENTS:
                continue
            if self.s['halted']:
                continue
            if inst in getattr(self, 'adopt_freed', ()):
                self.log('skip', inst, reason='adopted_closed_this_run', price_off_live=True)
                continue
            if any(m.get('status') == 'open' and m.get('inst') == inst for m in self.s['mirrored'].values()):
                self.log('skip', inst, reason='coin_already_mirrored', price_off_live=True)
                self.s['mirrored'][key] = dict(inst=inst, status='skipped', side=p['side'], posSide='net', price_off_live=True, reason='coin_already_mirrored')
                continue
            if self.held(inst, p['side']) or any(k[0] == inst for k in self.positions):
                self.log('skip', inst, reason='exchange_position_exists_not_ours', price_off_live=True)
                self.s['mirrored'][key] = dict(inst=inst, status='skipped', side=p['side'], posSide='net', price_off_live=True)
                continue
            if self.now_ms - int(p['opened']) > OPEN_WINDOW and action != 'follow-current' and not self.late_ok(inst, p):
                self.s['mirrored'][key] = dict(inst=inst, status='skipped', side=p['side'], posSide='net', reason='too_old', price_off_live=True)
                self.log('skip', inst, reason='paper_entry_too_old', price_off_live=True)
                continue
            self.open(key, inst, p)

    def pause_only(self):
        """Pause without the signals: set the flag first (it must hold even if the exchange cannot be read), then refresh
        the account for the summary. Sends no order, close or stop change."""
        self.s['paused'] = True
        self.log('pause', '-', reason='stale_signals')
        self.load()

    def settle(self):
        """load() ran before this run's orders: read the account again so the summary already lists new / closed positions
        (otherwise they only appear after the next scheduled run, 40–60 minutes later)."""
        if not self.dry and any(a['action'] in ('open', 'close', 'amend_stop', 'restore_target', 'amend_target', 'flatten', 'daily_halt') for a in self.actions):
            self.load()

    def summary(self):
        pos = [dict(inst=p['instId'], side=p.get('posSide', 'net'), contracts=p.get('pos'), avgPx=p.get('avgPx'), markPx=p.get('markPx'),
                    upl=p.get('upl'), uplRatio=p.get('uplRatio'), lever=p.get('lever'), margin=p.get('margin') or p.get('imr'), mgnMode=p.get('mgnMode'), ctVal=self.spec(p['instId'])['ctVal'],
                    ours=any(m['status'] == 'open' and m['inst'] == p['instId'] for m in self.s['mirrored'].values()),
                    **({'adopted': True} if self.adopted_of(p) else {}))
               for p in getattr(self, 'all_positions', self.positions).values()]
        return dict(updated_at=self.now.isoformat(timespec='seconds').replace('+00:00', 'Z'), equity=self.equity, available=self.available,
                    day_start=self.s.get('day_start'), paused=self.s['paused'], halted=self.s['halted'], dry_run=self.dry, positions=pos,
                    stop_control=self.s.get('stop_control'),
                    execution_profile=dict(ACTIVE_PROFILE or volume_profile(self.now), instruments=list(INSTRUMENTS), risk=RISK, same_side_cap=SAME_SIDE_CAP),
                    open_mirrors=[dict(m, key=k) for k, m in self.s['mirrored'].items() if m['status'] == 'open'],
                    start_equity=self.s.get('start_equity'), history=self.curve(),
                    closed_trades=sum(1 for m in self.s['mirrored'].values() if m['status'] == 'done'))

    def record_equity(self):
        """Equity curve for the dashboard: at most one stored point per HISTORY_STEP, capped."""
        self.s.setdefault('start_equity', self.equity)
        hist = self.s.setdefault('history', [])
        if not hist or self.now_ms - hist[-1][0] >= HISTORY_STEP:
            hist.append([self.now_ms, round(self.equity, 2)])
        del hist[:-HISTORY_POINTS]

    def curve(self):
        """Stored points plus the current equity, so the curve always ends at the latest run."""
        hist = self.s.get('history', [])[-HISTORY_POINTS:]
        if hasattr(self, 'equity') and (not hist or hist[-1][0] != self.now_ms):
            hist = hist + [[self.now_ms, round(self.equity, 2)]]
        return hist


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--action', default='run', choices=['run', 'dry-run', 'pause', 'resume', 'flatten', 'follow-current', 'snapshot'])
    args = ap.parse_args()
    mode = trading_mode()
    assert_mode(mode)
    forced_dry = dry_run_forced(mode)
    live = mode == 'live'
    signal_mode, _, _ = apply_signal_timing()
    print(f'信号模式 {signal_mode} · 信号最长 {LIVE_SIGNAL_MAX_AGE // 60_000} 分钟（实盘）· 新信号跟单窗口 {OPEN_WINDOW // 60_000} 分钟')
    print(f"OKX {'实盘' if live else '模拟盘'} · {'dry-run（只读账户、记录意图，不发送任何订单）' if forced_dry else '真实下单' if live else '模拟下单'}")
    creds = [os.environ.get(k, '').strip() for k in ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE')]
    if not all(creds):
        if live:
            raise SystemExit('未配置 OKX 实盘密钥（OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE 环境变量），未运行。')
        print('未配置 OKX 模拟交易密钥，跳过跟单。'); return
    # Signal source: the evolution champion's paper positions when present (tools/evolve), else the retired strategy engine.
    pause_only = False
    evolved_path, paper_path = args.data / 'evolve' / 'signals.json', args.data / 'combined' / 'state.json'
    if live and not evolved_path.exists():
        raise SystemExit('实盘只跟随进化冠军信号 evolve/signals.json；文件不存在，未运行。')
    if evolved_path.exists():
        sig = json.loads(evolved_path.read_text())
        age = signal_age_ms(sig, datetime.now(timezone.utc))
        stale = live and (age is None or age > LIVE_SIGNAL_MAX_AGE)
        # Hourly mode: a pause must work while the signals are stale (that is when it is wanted). It then only sets the
        # flag and reads the account; a normal pause also manages positions against the signals. github mode: unchanged.
        pause_only = stale and args.action == 'pause' and signal_mode == 'hourly'
        if stale and args.action != 'snapshot' and not pause_only:
            # A stale signal file would keep closed paper positions open and follow old entries; the exchange stops stay in place.
            raise SystemExit(f'evolve/signals.json 已过期、时间戳在未来或缺少 updated_at（超过 {LIVE_SIGNAL_MAX_AGE // 60_000} 分钟），实盘本轮不操作。')
        exits = sig.get('exits') if isinstance(sig.get('exits'), dict) else {}
        paper = {'positions': sig.get('positions', {}), 'exits': {k: v for k, v in exits.items() if isinstance(k, str) and isinstance(v, str)},
                 'source': 'evolve'}
        if live:                                   # PRICE_OFF LIVE: the live follower only; demo never reads it
            paper.update(price_off_live_fields(sig))
        applied = configure_evolved(sig.get('instruments'))
        print(f"跟随进化冠军 · 每笔风险按策略 {RISK_RANGE[0]:.0%}–{RISK_RANGE[1]:.0%} · 杠杆 1–{EVOLVED_MAX_LEVERAGE}×（按币种 OKX 上限，规格缺失则不开新仓）· 全仓 · 同向风险不设上限、≤ {MAX_SAME_SIDE} 仓 · 日亏 {DAILY_HALT:.0%} 停手 · {len(INSTRUMENTS)} 币")
    else:
        if not paper_path.exists():
            print('没有策略信号，跳过跟单。'); return
        paper = json.loads(paper_path.read_text())
        applied = configure_volume_test(universe=paper.get('universe') if paper.get('profile_mode') == 'aggressive' else None)
        print(describe_volume(applied))
    if args.action in ('run', 'resume', 'follow-current', 'dry-run') and applied['mode'] == 'aggressive' and len(INSTRUMENTS) != applied['scan_limit']:
        raise SystemExit('Aggressive execution requires the confirmed 100-contract signal universe; no orders submitted')
    out = state_dir(args.data, mode); out.mkdir(parents=True, exist_ok=True)
    state_path = out / 'state.json'
    state = load_state(state_path, mode)
    # dry-run: the requested action, or forced by the mode (live default). Snapshot is read-only either way.
    dry = args.action == 'dry-run' or (forced_dry and args.action != 'snapshot')
    if forced_dry and args.action not in ('run', 'dry-run', 'snapshot'):
        print(f'dry-run：{args.action} 只做预演，不保存状态、不发送订单。')
    mirror = Mirror(OKX(*creds, mode=mode, dry_run=forced_dry or dry), json.loads(json.dumps(state)) if dry else state, paper,
                    datetime.now(timezone.utc), dry, adopt=adopt_config())
    if applied.get('mode') == 'evolved' and not live:
        mirror.live = real_price      # live trades the real market already: nothing to compare against
    if applied.get('mode') == 'evolved' and live:
        mirror.follow_price_off = True    # PRICE_OFF LIVE: only the live account follows signals the demo price check refused
    error = None
    try:
        if args.action == 'snapshot':
            mirror.load()  # Read-only exchange refresh: no order, leverage or stop mutations.
        elif pause_only:
            print('信号已过期：只设置暂停，不按信号管理仓位（交易所止损 / 止盈仍在）')
            mirror.pause_only()
        else:
            mirror.run('run' if dry else args.action)
            mirror.settle()
    except OKXError as exc:
        error = str(exc); mirror.log('error', '-', step='run', detail=error)
    for a in mirror.actions:
        print(json.dumps(a, ensure_ascii=False))
    if dry and live:
        # Evidence for switching on: what this run would have done. Account numbers and intended orders only, no credentials.
        atomic_json(out / 'dry_run_preview.json', dict(updated_at=mirror.now.isoformat(timespec='seconds').replace('+00:00', 'Z'),
                    mode=mode, dry_run=True, action=args.action, equity=getattr(mirror, 'equity', None), actions=mirror.actions))
    if not dry and hasattr(mirror, 'equity'):
        mirror.record_equity()
    if not dry:
        atomic_json(state_path, state)
        with (out / 'actions.jsonl').open('a') as f:
            f.writelines(json.dumps(a, ensure_ascii=False) + '\n' for a in mirror.actions)
        if hasattr(mirror, 'equity'):
            summary = mirror.summary()
            if live:
                summary['trading_mode'] = 'live'
            summary.update(decision_snapshot(args.data))
            lines = (out / 'actions.jsonl').read_text().splitlines()[-30:]
            summary['recent_actions'], tests_closed = dashboard_actions(out, [json.loads(l) for l in lines])
            enrich_closes(mirror, state, summary['recent_actions'])
            for a in summary['recent_actions']:      # the biggest single close so far, kept so the dashboard can put the headline return in context
                if a.get('action') in ('close', 'closed_on_exchange') and isinstance(a.get('pnl'), (int, float)) \
                        and abs(a['pnl']) > abs((state.get('largest_close') or {}).get('pnl', 0)):
                    state['largest_close'] = dict(ts=a['ts'], inst=a['inst'], pnl=a['pnl'])
            summary['largest_close'] = state.get('largest_close')
            atomic_json(state_path, state)  # keep looked-up close results so each is fetched once
            summary['execution_test_trades'] = tests_closed
            summary['closed_trades'] += tests_closed
            atomic_json(out / 'summary.json', summary)
    if error:
        sys.exit(1)


if __name__ == '__main__':
    main()

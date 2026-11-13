"""Shared execution: the ONE bar-by-bar engine used by both backtests and paper trading (同源执行).

Rules (identical in every caller):
- Signals are computed on a closed bar; the order fills at the NEXT bar's open, with slippage and a taker fee.
- One position per instrument and at most MAX_POSITIONS at once; size = equity × risk ÷ stop distance.
- Contract mode (the same lot rules as the OKX mirror, tools/okx_demo_trader.py): the size is rounded down to whole lots of
  the coin's contract (`Series.spec` = ctVal, lotSz, minSz) and skipped below the minimum; leverage is the genome's
  leverage gene (1–20×, default 10× for old genomes), then clamped to that coin's OKX max leverage (static snapshot of
  public `lever`, 20× if unknown; every coin is also capped at LEVERAGE = 20×). Margin mode is cross (全仓): initial margin = notional ÷ leverage
  is reserved from the shared account, and an entry is skipped when its margin exceeds 95 % of the free margin
  (equity − margin in use). Stops closer than 0.2 % are skipped. Liquidation is at the account level: when mark equity
  (cash + unrealized PnL of every open position, after fees and funding) is at or below total maintenance margin
  (sum of mark notional × MMR, 0.5 % on MAJORS and 2.5 % on others), every position is closed at that bar's price
  with taker fees (reason `liquidation`). There is no partial or tiered liquidation — one breach wipes the book
  (cash is floored at 0 if a gap goes past bankruptcy).
  A new entry is skipped when its own stop (fees and slippage included), or the worst case of every open stop at once,
  would leave equity at or below the remaining maintenance margin. Funding (`Series.funding`, {ts: rate}) is paid /
  received on every funding time a position is held: notional × rate (not margin × rate), longs pay positive rates, and
  is deducted from shared cash, so it can itself trigger a cross liquidation.
- Stop and target are checked inside each later bar; if a bar touches both, the stop is assumed first (conservative);
  a gap through the stop fills at the open. Positions also exit at the close after `max_hold` bars.
- Equity is marked to market at every close; max drawdown is measured on that curve.
- Timeframe gene `tf` (1 / 4 / 24 h): signals and the stop's ATR come from 4H / 1D bars built from the 1H bars (UTC
  aligned, complete bars only) and are decided when such a bar closes; fills, stops and targets still run on 1H bars.
- Gene `trail` = 1: after each bar the stop follows the best price at the original stop distance (never loosens).
"""
import json
import math
from pathlib import Path

FEE = .0005          # taker fee per side
SLIP = .0002         # slippage per fill
LEVERAGE = 20        # most leverage a gene may request (paper account; the OKX demo mirror still has its own cap)
DEFAULT_LEVERAGE = 10  # genomes saved before the leverage gene
MIN_LEVERAGE = 1
# Maintenance margin rate (MMR) used for cross-margin liquidation (account equity vs total MM).
# 0.5 % is in the range of OKX / Binance lowest-tier rates for the most liquid USDT perps.
# Thinner coins use 2.5 %, a conservative stand-in for higher official tiers (we do not model OKX's full
# notional-tier schedule). Liquidation closes the whole book when equity <= sum(notional × MMR); there is
# no partial or tiered liquidation. A new entry is skipped if its stop would leave equity at or below MM.
MMR_MAJOR = .005
MMR_OTHER = .025
MMR = MMR_MAJOR      # default for helpers called without an instrument
# One shared major-coin list for paper and the OKX demo follower: risk is doubled (then capped at 10 %),
# and MMR is 0.5 % (every other coin uses 2.5 %). BTC/ETH/SOL/XRP typically allow 100× on OKX
# and BNB/DOGE 50×, but every gene is capped at LEVERAGE (20×) first, then the per-instrument cap applies. A major missing from the leverage
# snapshot (paper) or the public instruments response (demo) falls back to 20×, same as unknown alts.
MAJORS = ('BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'BNB-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP')
FALLBACK_INST_LEVERAGE = 20   # unknown coins in the paper engine and the OKX demo follower
MARGIN_MODE = 'cross'        # paper and the OKX demo follower both use cross margin (全仓)
MARGIN_USE = .95     # an entry may use at most this share of the free margin
MIN_STOP_DIST = .002 # stops closer than this (fraction of price) are skipped, as on OKX
START_EQUITY = 10000.0
MAX_HOLD_H = 720     # longest hold in hours, whatever the timeframe
MAX_POSITIONS = 3    # open positions per strategy at the same time; extra signals wait until one closes
HOUR = 3600_000


# ----------------------------------------------------------------- indicators (cached per series)
class Series:
    """One instrument's 1H candles (volume optional) with lazily cached indicators. `ref` is the BTC series on the
    same timeline (relative strength); the BTC series itself has no ref."""
    def __init__(self, inst, ts, o, h, l, c, v=None, ref=None, spec=None, funding=None):
        self.inst, self.ts, self.o, self.h, self.l, self.c = inst, ts, o, h, l, c
        self.v, self.ref = v, ref
        self.spec, self.funding = spec, funding     # (ctVal, lotSz, minSz) and {ts: funding rate}; None = continuous size / no funding
        self.n = len(ts)
        self._cache = {}

    def tf(self, hours):
        """(bars, closes_at, last_closed) for a timeframe of `hours`: bars is a Series of complete UTC-aligned bars;
        closes_at[i] is the bar that closes with 1H bar i (else -1); last_closed[i] the latest bar closed by i (else -1)."""
        def build():
            groups, cur = [], None
            for i, t in enumerate(self.ts):
                k = t // (hours * HOUR)
                if cur is None or cur[0] != k:
                    cur = [k, i, i]; groups.append(cur)
                else:
                    cur[2] = i
            full = [(a, b) for k, a, b in groups if b - a + 1 == hours and self.ts[b] - self.ts[a] == (hours - 1) * HOUR]
            closes_at, last_closed = [-1] * self.n, [-1] * self.n
            for j, (a, b) in enumerate(full):
                closes_at[b] = j
            last = -1
            for i in range(self.n):
                if closes_at[i] >= 0:
                    last = closes_at[i]
                last_closed[i] = last
            bars = Series(self.inst, [self.ts[a] for a, b in full], [self.o[a] for a, b in full],
                          [max(self.h[a:b + 1]) for a, b in full], [min(self.l[a:b + 1]) for a, b in full], [self.c[b] for a, b in full],
                          [sum(self.v[a:b + 1]) for a, b in full] if self.v else None)
            if self.ref is not None:
                bars.ref = self.ref.tf(hours)[0]
            return bars, closes_at, last_closed
        if hours == 1:
            return self._get(('tf', 1), lambda: (self, list(range(self.n)), list(range(self.n))))
        return self._get(('tf', hours), build)

    def close_index(self, hours):
        """For each bar j of the `hours` timeframe, the 1H index at which it closes."""
        def build():
            closes_at = self.tf(hours)[1]
            out = [0] * self.tf(hours)[0].n
            for i, j in enumerate(closes_at):
                if j >= 0:
                    out[j] = i
            return out
        return self._get(('cidx', hours), build)

    def vol_avg(self, n):
        """Average volume of the n bars BEFORE i (None without volume or history)."""
        def build():
            out, s = [None] * self.n, 0.0
            for i in range(self.n):
                if i >= n:
                    out[i] = s / n
                    s -= self.v[i - n]
                s += self.v[i]
            return out
        return self._get(('vavg', n), build)

    def _get(self, key, build):
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    def ema(self, n):
        def build():
            k, out, v = 2 / (n + 1), [], None
            for x in self.c:
                v = x if v is None else v + k * (x - v)
                out.append(v)
            return out
        return self._get(('ema', n), build)

    def atr(self, n=14):
        def build():
            out, v = [], None
            for i in range(self.n):
                tr = self.h[i] - self.l[i] if i == 0 else max(self.h[i] - self.l[i], abs(self.h[i] - self.c[i - 1]), abs(self.l[i] - self.c[i - 1]))
                v = tr if v is None else v + (tr - v) / n
                out.append(v)
            return out
        return self._get(('atr', n), build)

    def vol_ratio(self):
        """ATR% relative to its own slow baseline (≈ 3 weeks): >1 means more volatile than usual."""
        def build():
            atr, out, base = self.atr(), [], None
            for i in range(self.n):
                pct = atr[i] / self.c[i]
                base = pct if base is None else base + (pct - base) / 500
                out.append(pct / base if base > 0 else 1.0)
            return out
        return self._get(('volr',), build)

    def donchian(self, n):
        """Highest high / lowest low of the n bars BEFORE i (no look-ahead)."""
        def build():
            hi, lo = [math.inf] * self.n, [-math.inf] * self.n
            from collections import deque
            dh, dl = deque(), deque()
            for i in range(self.n):
                while dh and dh[0] < i - n: dh.popleft()
                while dl and dl[0] < i - n: dl.popleft()
                if i >= n:
                    hi[i], lo[i] = self.h[dh[0]], self.l[dl[0]]
                while dh and self.h[dh[-1]] <= self.h[i]: dh.pop()
                while dl and self.l[dl[-1]] >= self.l[i]: dl.pop()
                dh.append(i); dl.append(i)
            return hi, lo
        return self._get(('don', n), build)

    def zscore(self, n):
        def build():
            out, s, s2 = [0.0] * self.n, 0.0, 0.0
            for i, x in enumerate(self.c):
                s += x; s2 += x * x
                if i >= n:
                    y = self.c[i - n]; s -= y; s2 -= y * y
                if i >= n - 1:
                    m = s / n; var = max(s2 / n - m * m, 1e-18)
                    out[i] = (x - m) / math.sqrt(var)
            return out
        return self._get(('z', n), build)


# ----------------------------------------------------------------- signals
def signal(g, s, i):
    """+1 long, -1 short, 0 none, decided on the CLOSE of 1H bar i (data up to i only). A 4H / 1D genome only
    decides when one of its bars closes at i, from those bars."""
    if i < 2:
        return 0
    vr = s.vol_ratio()[i]
    if vr < g['vol_min'] or vr > g['vol_max']:      # volatility regime filter (the genome's own view)
        return 0
    x, closes_at, _ = s.tf(g.get('tf', 1))
    j = closes_at[i]
    if j < 2:
        return 0
    return _species_signal(g, x, j)


def stop_distance(g, s, i):
    """Stop distance for a signal decided at the close of 1H bar i: stop_atr × ATR of the genome's timeframe."""
    x, _, last_closed = s.tf(g.get('tf', 1))
    j = last_closed[i]
    return g['stop_atr'] * x.atr()[j] if j >= 0 else 0.0


def _raw_signals(g, x):
    """[(j, d)] for every bar j >= 2 of series x where the species fires, before the direction filter. Same rules as
    _species_signal, evaluated for all bars at once (the hot path of every backtest)."""
    sp, n, c = g['species'], x.n, x.c
    if sp == 'momentum':
        hi, lo = x.donchian(g['lookback'])
        return [(j, 1 if c[j] > hi[j] else -1) for j in range(2, n) if c[j] > hi[j] or c[j] < lo[j]]
    if sp == 'mean_revert':
        z, th = x.zscore(g['lookback']), g['threshold']
        return [(j, 1 if z[j] < -th else -1) for j in range(2, n) if z[j] < -th or z[j] > th]
    if sp == 'vol_breakout':
        atr, h, l, e, out = x.atr(), x.h, x.l, g['expand'], []
        for j in range(2, n):
            a, rng = atr[j - 1], h[j] - l[j]
            if a > 0 and rng > e * a:
                pos = (c[j] - l[j]) / rng
                if pos > .75: out.append((j, 1))
                elif pos < .25: out.append((j, -1))
        return out
    if sp == 'trend':
        f, sl = x.ema(g['fast']), x.ema(g['slow'])
        return [(j, 1 if f[j] > sl[j] else -1) for j in range(2, n)
                if f[j] > sl[j] and f[j - 1] <= sl[j - 1] or f[j] < sl[j] and f[j - 1] >= sl[j - 1]]
    if sp == 'vol_surge':
        if not x.v:
            return []
        avg, v, m, o = x.vol_avg(g['lookback']), x.v, g['vol_mult'], x.o
        return [(j, 1 if c[j] > o[j] else -1) for j in range(2, n) if avg[j] and v[j] > m * avg[j] and c[j] != o[j]]
    if sp == 'rel_strength':
        k, r, th = g['lookback'], x.ref, g['rs_th']
        if r is None:
            return []
        rc, out = r.c, []
        for j in range(max(2, k + 1), n):
            rs = c[j] / c[j - k] - rc[j] / rc[j - k]
            if rs > th or rs < -th:
                prev = c[j - 1] / c[j - 1 - k] - rc[j - 1] / rc[j - 1 - k]
                if rs > th >= prev: out.append((j, 1))
                elif rs < -th <= prev: out.append((j, -1))
        return out
    return []


def entries(g, series):
    """All entry signals of genome g on every series, as {1H index i: [(inst, d, stop distance)]} in series order.
    Equivalent to calling signal() and stop_distance() on every bar, but computed once per genome."""
    tf, out = g.get('tf', 1), {}
    want = {'long': 1, 'short': -1}.get(g['direction'])
    lo, hi, k = g['vol_min'], g['vol_max'], g['stop_atr']
    for inst, s in series.items():
        x = s.tf(tf)[0]
        cidx, vr, atr = s.close_index(tf), s.vol_ratio(), x.atr()
        for j, d in _raw_signals(g, x):
            if want and d != want:
                continue
            i = cidx[j]
            if i < 2 or vr[i] < lo or vr[i] > hi:
                continue
            out.setdefault(i, []).append((inst, d, k * atr[j]))
    return out


def _species_signal(g, s, i):
    sp, d = g['species'], 0
    if sp == 'momentum':
        hi, lo = s.donchian(g['lookback'])
        d = 1 if s.c[i] > hi[i] else -1 if s.c[i] < lo[i] else 0
    elif sp == 'mean_revert':
        z = s.zscore(g['lookback'])[i]
        d = 1 if z < -g['threshold'] else -1 if z > g['threshold'] else 0
    elif sp == 'vol_breakout':
        atr, rng = s.atr()[i - 1], s.h[i] - s.l[i]
        if atr > 0 and rng > g['expand'] * atr:
            pos = (s.c[i] - s.l[i]) / rng if rng > 0 else .5
            d = 1 if pos > .75 else -1 if pos < .25 else 0
    elif sp == 'trend':
        f, sl = s.ema(g['fast']), s.ema(g['slow'])
        d = 1 if f[i] > sl[i] and f[i - 1] <= sl[i - 1] else -1 if f[i] < sl[i] and f[i - 1] >= sl[i - 1] else 0
    elif sp == 'vol_surge':
        avg = s.vol_avg(g['lookback'])[i] if s.v else None
        if avg and s.v[i] > g['vol_mult'] * avg:
            d = 1 if s.c[i] > s.o[i] else -1 if s.c[i] < s.o[i] else 0
    elif sp == 'rel_strength':
        n, r = g['lookback'], s.ref
        if r is not None and i - 1 >= n:
            rs = s.c[i] / s.c[i - n] - r.c[i] / r.c[i - n]
            prev = s.c[i - 1] / s.c[i - 1 - n] - r.c[i - 1] / r.c[i - 1 - n]
            d = 1 if rs > g['rs_th'] >= prev else -1 if rs < -g['rs_th'] <= prev else 0
    if g['direction'] == 'long' and d < 0 or g['direction'] == 'short' and d > 0:
        return 0
    return d



# ----------------------------------------------------------------- cross-margin liquidation
def maintenance_rate(inst):
    """Per-coin MMR: 0.5 % for MAJORS (BTC/ETH/SOL/BNB/XRP/DOGE), 2.5 % for every other coin."""
    return MMR_MAJOR if inst in MAJORS else MMR_OTHER


def _load_inst_caps():
    """Static snapshot of OKX USDT-swap `lever` (tools/evolve/okx_swap_lever.json). Empty if the file is missing."""
    path = Path(__file__).with_name('okx_swap_lever.json')
    try:
        data = json.loads(path.read_text())
        raw = data.get('lever') or {}
        return {str(k): int(v) for k, v in raw.items() if int(v) >= 1}
    except (OSError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return {}


INST_MAX_LEVERAGE = _load_inst_caps()


def inst_max_leverage(inst, cap=LEVERAGE):
    """Highest leverage this coin may use on the paper account, consistent with the OKX demo follower.

    Uses the static OKX `lever` snapshot when the instrument is listed, then caps at `cap` (LEVERAGE, 20×).
    Any coin missing from the snapshot — majors included — falls back to 20×, the same default as the demo follower.
    """
    cap = max(MIN_LEVERAGE, int(cap))
    listed = INST_MAX_LEVERAGE.get(inst)
    if listed and listed >= 1:
        return max(MIN_LEVERAGE, min(int(listed), cap))
    return max(MIN_LEVERAGE, min(FALLBACK_INST_LEVERAGE, cap))


def liquidation_price(entry, side, leverage=None, mmr=MMR, fee=FEE, margin=None, qty=None):
    """Informational isolated-style liquidation price (not used for execution).

    Cross-margin liquidation is at the account level (see `book_liquidated`). This helper is kept so a
    single-position estimate can still be shown or compared: it is the price at which *this* position
    alone would exhaust its own initial margin after MMR and the close fee.
    long:  entry × (1 − (cushion − MMR − fee))
    short: entry × (1 + (cushion − MMR − fee))
    `cushion` is remaining margin ÷ entry notional (equal to 1/L when funding has not yet moved the margin).
    side is +1 for a long, −1 for a short. Leverage is at least 1.
    """
    if margin is not None and qty is not None and entry:
        cushion = margin / (qty * entry)
    else:
        L = max(MIN_LEVERAGE, float(leverage if leverage is not None else MIN_LEVERAGE))
        cushion = 1.0 / L
    return entry * (1 - side * (cushion - mmr - fee))


def max_leverage_for_stop(stop_frac, mmr=MMR, fee=FEE, cap=LEVERAGE):
    """Highest integer leverage at which an *isolated* stop would sit strictly before `liquidation_price`.

    Informational only: cross-margin execution does not reduce leverage this way. Kept so older tests
    and callers can still ask for the isolated estimate.
    """
    room = stop_frac + mmr + fee
    cap = max(MIN_LEVERAGE, int(cap))
    if room <= 0:
        return cap
    return max(MIN_LEVERAGE, min(cap, int(1.0 / room - 1e-12)))


def clamp_leverage(desired, stop_frac=None, cap=LEVERAGE, mmr=None, fee=FEE):
    """Requested leverage, capped to [1, `cap`].

    Cross margin: leverage only sets initial margin (notional / L). It no longer changes a
    per-position liquidation price. `stop_frac`, `mmr` and `fee` are accepted for call-site
    compatibility and ignored; the stop-before-liquidation rule is `cross_open_allowed`.
    The per-coin OKX cap is applied by the caller via `inst_max_leverage` / the live `lever`.
    """
    try:
        want = int(desired)
    except (TypeError, ValueError):
        want = cap
    return max(MIN_LEVERAGE, min(want, int(cap)))


def isolated_remaining_margin(p):
    """Legacy helper: isolated margin left after funding (never negative). Not used by the cross engine."""
    return max(0.0, p['margin'] - p.get('funding', 0.0))


def apply_isolated_funding(p, pay, inst=None):
    """Legacy helper: charge funding against isolated margin and refresh the informational liq estimate."""
    p['funding'] = p.get('funding', 0.0) + pay
    mmr = p.get('mmr', maintenance_rate(inst))
    p['liq'] = liquidation_price(p['entry'], p['side'], p.get('lever'), mmr=mmr, fee=FEE,
                                 margin=isolated_remaining_margin(p), qty=p['qty'])
    return p['liq']


def isolated_liq_cash(p):
    """Legacy helper: cash change and net PnL of an isolated liquidation (wipe remaining margin)."""
    remaining = isolated_remaining_margin(p)
    return -remaining, -p['margin'] - p['fee']


def book_equity(cash, pos, prices):
    """Mark equity: cash + unrealized PnL of every open position (entry fees and funding already in cash)."""
    return cash + sum(p['qty'] * (prices[inst] - p['entry']) * p['side'] for inst, p in pos.items())


def book_mm(pos, prices):
    """Total maintenance margin: sum of mark notional × that coin's MMR."""
    return sum(p['qty'] * max(prices[inst], 0.0) * p.get('mmr', maintenance_rate(inst)) for inst, p in pos.items())


def book_liquidated(cash, pos, prices):
    """True when the book is non-empty and mark equity is at or below total maintenance margin."""
    return bool(pos) and book_equity(cash, pos, prices) <= book_mm(pos, prices)


def close_proceeds(p, px, slip=False):
    """Close `p` at `px`. Returns (fill_price, cash_delta, net_pnl). Always charges the taker fee.

    `slip=True` applies the usual exit slippage away from the trader. Cross liquidation uses the
    bar price with fees and no extra slip (`slip=False`).
    """
    if slip:
        px = px * (1 - SLIP * p['side'])
    fee = p['qty'] * px * FEE
    gain = p['qty'] * (px - p['entry']) * p['side'] - fee
    net = gain - p['fee'] - p.get('funding', 0.0)
    return px, gain, net


def liquidate_book(cash, pos, prices, ts, trades, extra=None):
    """Full-account cross liquidation: close every position at `prices` with fees, reason `liquidation`.

    No partial or tiered liquidation is modelled — one breach closes the whole book. A gap far through the bankruptcy
    price cannot leave the account below zero (the exchange's insurance fund takes the rest), so cash is floored at 0.
    `extra(inst, p)` may add fields (for example `seg` / `boost` on the shared account). Returns the new cash balance.
    """
    for inst in list(pos):
        p = pos.pop(inst)
        px, gain, net = close_proceeds(p, prices[inst], slip=False)
        cash += gain
        rec = dict(inst=inst, side=p['side'], entry=p['entry'], exit=px, opened=p['opened'], closed=ts,
                   pnl=net, funding=round(p.get('funding', 0.0), 4), qty=p['qty'], lever=p['lever'],
                   reason='liquidation')
        if extra:
            rec.update(extra(inst, p))
        trades.append(rec)
    return max(cash, 0.0)


def _all_stops_leftover(cash, pos):
    """Cash left if every open position is stopped (with exit slippage and fees)."""
    left = cash
    for p in pos.values():
        _, gain, _ = close_proceeds(p, p['stop'], slip=True)
        left += gain
    return left


def cross_open_allowed(cash, pos, inst, new_p, prices, fixed=None):
    """Whether `new_p` may be opened on this cross-margin book.

    `cash` is the balance *before* the new entry fee, without the unrealized PnL of `pos` / `fixed`; `new_p['fee']` is
    the entry fee. `prices` marks every key of `pos` and `fixed` (typically the bar's open / the last price).
    `fixed` = positions that share the account but have no stop this program manages (manual positions on the OKX demo
    account): they stay at the mark in every scenario and keep their maintenance margin. Returns (ok, reason) with
    reason None on success, otherwise `equity_below_mm` or `stop_before_liquidation`.

    Rules (strict inequalities):
    1. After the entry, mark equity must stay above total MM including the new position.
    2. If the new position is then stopped (slip + fee) and the others stay at the mark, remaining
       equity must stay above the remaining MM; a lone position must also leave cash > 0.
    3. If every stopped position, including the new one, is stopped at once (fixed ones at the mark), equity must stay
       above the remaining MM (cash > 0 when nothing is left).
    Leverage is not reduced to satisfy these rules — the entry is skipped.
    """
    fixed = dict(fixed or {})
    cash_after = cash - new_p['fee']
    book = dict(pos)
    book.update(fixed)
    book[inst] = new_p
    px_book = dict(prices)
    px_book[inst] = new_p['entry']
    if book_liquidated(cash_after, book, px_book):
        return False, 'equity_below_mm'
    _, gain, _ = close_proceeds(new_p, new_p['stop'], slip=True)
    cash_stopped = cash_after + gain
    others = dict(pos)
    others.update(fixed)
    if others:
        if book_liquidated(cash_stopped, others, {k: prices[k] for k in others}):
            return False, 'stop_before_liquidation'
    elif cash_stopped <= 0:
        return False, 'stop_before_liquidation'
    left = _all_stops_leftover(cash_after, dict(pos, **{inst: new_p}))
    if fixed:
        if book_liquidated(left, fixed, {k: prices[k] for k in fixed}):
            return False, 'stop_before_liquidation'
    elif left <= 0:
        return False, 'stop_before_liquidation'
    return True, None


def marks_at(series, keys, i, field):
    """{inst: series[inst].field[i]} for inst in keys."""
    return {k: getattr(series[k], field)[i] for k in keys}


def adverse_prices(pos, series, i):
    """Conservative same-bar extremes: lows for longs, highs for shorts."""
    out = {}
    for inst, p in pos.items():
        s = series[inst]
        out[inst] = s.l[i] if p['side'] == 1 else s.h[i]
    return out


# ----------------------------------------------------------------- the engine
def simulate(g, series, start, end, equity=START_EQUITY, close_at_end=False, curve_every=24, events=None, stop_entries_at=None,
             risk=None, lever_cap=None, extra_equity=None):
    """Run genome g over bar indexes [start, end) of every series (all series share the same timeline).
    `events` = entries(g, series), when the caller already computed them.
    `stop_entries_at`: from this bar index on no new signals are taken; open positions run to their own exits
    (a member that lost its seat winds down instead of being closed at market).
    `risk` / `lever_cap` override the genome's risk gene and leverage gene (still capped by MAX_TRADE_RISK / LEVERAGE),
    and `extra_equity` ({ts: USDT}, carried forward) is equity outside this run that sizing and the margin check may
    count on (legacy slot accounting in paper.py).

    Returns dict(equity, trades, curve, positions, metrics). `positions` are still open at `end` unless close_at_end.
    """
    insts = list(series)
    ts0 = series[insts[0]].ts
    if events is None:
        events = entries(g, series)
    cash, pos, trades, curve = equity, {}, [], []
    peak, max_dd, pending, dists = equity, 0.0, {}, {}
    rr, trail = g['target_rr'], g.get('trail', 0)
    risk = min(g['risk'] if risk is None else risk, MAX_TRADE_RISK)
    desired_lever = LEVERAGE if lever_cap is None else lever_cap
    if lever_cap is None:
        desired_lever = g.get('leverage', DEFAULT_LEVERAGE)
    cap, extra = min(int(desired_lever), LEVERAGE), 0.0
    hold = min(g['max_hold'] * g.get('tf', 1), MAX_HOLD_H)
    for i in range(start, end):
        if extra_equity is not None:
            extra = extra_equity.get(ts0[i], extra)
        def _liq_now(prices):
            nonlocal cash
            if pos and book_liquidated(cash + extra, pos, prices):
                cash = liquidate_book(cash, pos, prices, ts0[i], trades)
                return True
            return False
        # 0) funding at this hour, on positions held into it (shared cash; may itself trigger cross liquidation)
        for inst, p in pos.items():
            f = series[inst].funding
            if f:
                rate = f.get(ts0[i])
                if rate:
                    pay = p['qty'] * series[inst].o[i] * rate * p['side']
                    cash -= pay
                    p['funding'] = p.get('funding', 0.0) + pay
                    # informational isolated-style estimate only
                    p['liq'] = liquidation_price(p['entry'], p['side'], p.get('lever'), mmr=p.get('mmr', maintenance_rate(inst)),
                                                 fee=FEE, margin=isolated_remaining_margin(p), qty=p['qty'])
        opens = marks_at(series, list(pos), i, 'o')
        if _liq_now(opens):
            pending, dists = {}, {}
        # 1) fills at this bar's open for signals decided on the previous close
        for inst, d in pending.items():
            s = series[inst]
            dist = dists.get(inst, 0.0)
            if dist <= 0 or inst in pos or len(pos) >= MAX_POSITIONS:
                continue
            px = s.o[i] * (1 + SLIP * d)
            if dist / px < MIN_STOP_DIST:
                continue
            mark = cash + sum(p['qty'] * (series[k].o[i] - p['entry']) * p['side'] for k, p in pos.items()) + extra
            qty = mark * risk / dist
            if s.spec:
                ct, lot, mn = s.spec
                qty = math.floor(qty / ct / lot + 1e-9) * lot
                if qty <= 0 or qty < mn - 1e-12:
                    continue
                qty *= ct
            mmr = maintenance_rate(inst)
            inst_cap = inst_max_leverage(inst, cap=LEVERAGE)
            lever = clamp_leverage(cap, cap=inst_cap)
            margin = qty * px / lever
            if qty <= 0 or margin > (mark - sum(p['margin'] for p in pos.values())) * MARGIN_USE:
                continue
            new_p = dict(side=d, entry=px, qty=qty, stop=px - d * dist, target=px + d * rr * dist if rr > 0 else None,
                         opened=s.ts[i], bar=i, fee=qty * px * FEE, dist=dist, lever=lever, margin=margin,
                         liq=liquidation_price(px, d, lever, mmr=mmr), funding=0.0, mmr=mmr)
            opens_now = marks_at(series, list(pos), i, 'o')
            ok, _why = cross_open_allowed(cash, pos, inst, new_p, opens_now)
            if not ok:
                continue
            cash -= new_p['fee']
            pos[inst] = new_p
        pending, dists = {}, {}
        opens = marks_at(series, list(pos), i, 'o')
        _liq_now(opens)
        # 2) exits inside this bar (stops, targets, time — no per-position isolated liquidation)
        for inst in list(pos):
            p, s = pos[inst], series[inst]
            o, h, l, c = s.o[i], s.h[i], s.l[i], s.c[i]
            exit_px, why = None, None
            if p['side'] == 1:
                if o <= p['stop']: exit_px, why = o, 'stop'
                elif l <= p['stop']: exit_px, why = p['stop'], 'stop'
                elif p['target'] and o >= p['target']: exit_px, why = o, 'target'
                elif p['target'] and h >= p['target']: exit_px, why = p['target'], 'target'
            else:
                if o >= p['stop']: exit_px, why = o, 'stop'
                elif h >= p['stop']: exit_px, why = p['stop'], 'stop'
                elif p['target'] and o <= p['target']: exit_px, why = o, 'target'
                elif p['target'] and l <= p['target']: exit_px, why = p['target'], 'target'
            if exit_px is None and i - p['bar'] >= hold:
                exit_px, why = c, 'time'
            if exit_px is None and close_at_end and i == end - 1:
                exit_px, why = c, 'switch'
            if exit_px is not None:
                exit_px, gain, net = close_proceeds(p, exit_px, slip=True)
                cash += gain
                trades.append(dict(inst=inst, side=p['side'], entry=p['entry'], exit=exit_px, opened=p['opened'], closed=s.ts[i],
                                   pnl=net, funding=round(p['funding'], 4), qty=p['qty'], lever=p['lever'], reason=why,
                                   liq=p.get('liq')))
                del pos[inst]
            elif trail:                                     # after the bar: follow the best price, never loosen
                p['stop'] = max(p['stop'], h - p['dist']) if p['side'] == 1 else min(p['stop'], l + p['dist'])
        # 2b) conservative same-bar check: all remaining positions at their adverse extreme
        if pos:
            _liq_now(adverse_prices(pos, series, i))
        # 3) mark to market (and a close-price cross-liquidation check), drawdown, new signals
        closes = marks_at(series, list(pos), i, 'c')
        _liq_now(closes)
        eq = cash + sum(p['qty'] * (series[k].c[i] - p['entry']) * p['side'] for k, p in pos.items())
        peak = max(peak, eq); max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0)
        if curve_every and (i - start) % curve_every == 0:
            curve.append((ts0[i], round(eq, 2)))
        if (i < end - 1 or not close_at_end) and (stop_entries_at is None or i < stop_entries_at):
            for inst, d, dist in events.get(i, ()):
                if inst not in pos:
                    pending[inst], dists[inst] = d, dist
    last = end - 1
    eq = cash + sum(p['qty'] * (series[k].c[last] - p['entry']) * p['side'] for k, p in pos.items())
    if curve_every and (not curve or curve[-1][0] != series[insts[0]].ts[last]):
        curve.append((series[insts[0]].ts[last], round(eq, 2)))
    wins = sum(1 for t in trades if t['pnl'] > 0)
    gross_win = sum(t['pnl'] for t in trades if t['pnl'] > 0); gross_loss = -sum(t['pnl'] for t in trades if t['pnl'] < 0)
    metrics = dict(ret=round((eq / equity - 1) * 100, 3), max_dd=round(max_dd * 100, 3), trades=len(trades),
                   win_rate=round(wins / len(trades) * 100, 1) if trades else 0.0,
                   profit_factor=round(gross_win / gross_loss, 3) if gross_loss > 0 else (None if not trades else 99.0))
    open_positions = [dict(inst=k, side=p['side'], entry=p['entry'], qty=p['qty'], stop=p['stop'], target=p['target'], opened=p['opened'],
                           lever=p['lever'], margin=round(p['margin'], 4), liq=p['liq'], funding=round(p['funding'], 4))
                      for k, p in pos.items()]
    return dict(equity=eq, cash=cash, trades=trades, curve=curve, positions=open_positions, metrics=metrics, pending=pending)


def fitness(m):
    """Training fitness: return per unit of drawdown (Calmar-like), damped when there are too few trades."""
    dd = max(m['max_dd'], 5.0)
    return round(m['ret'] / dd * min(1.0, m['trades'] / 30), 4)


# ----------------------------------------------------------------- the shared (cross-margin) team account
# major_factor: a trade on one of MAJORS risks this many times the usual amount (the most liquid coins, and the ones
# whose OKX demo prices follow the real market). The same MAJORS list sets the 0.5 % MMR tier.
# Per-trade risk never exceeds 10 % of equity, for every seat including the leader (the gene itself is 2–10 %).
# Leverage is chosen by the gene (1–20×) and then clamped to that coin's OKX max; it only sets initial margin.
# A new entry is skipped when its stop (or every open stop at once) would leave mark equity at or below total MM.
# major_factor still doubles a major-coin entry, then the 10 % cap is applied so a doubled 8 % gene becomes 10 %, not 16 %.
# leader_risk / leader_lever are the same caps: the leader uses its own risk and leverage genes, not a fixed bigger size.
MAX_TRADE_RISK = .10
MIN_TRADE_RISK = .02
ACCOUNT_RULES = dict(leader_risk=MAX_TRADE_RISK, leader_lever=LEVERAGE, lever=LEVERAGE, same_side_cap=.20, daily_halt=.10,
                     day_shift=7 * HOUR, majors=MAJORS, major_factor=2.0, max_risk=MAX_TRADE_RISK, min_risk=MIN_TRADE_RISK,
                     min_lever=MIN_LEVERAGE, margin_mode=MARGIN_MODE)
CROWD_STEPS = ((6, .25), (3, .5))     # same-side positions already open → risk factor for the next entry on that side


def crowd_factor(same_side_open):
    """Many positions on one side are one bet: with 3 or more already open on that side a new entry risks half, with 6 or
    more a quarter (everyone, the leader included). The OKX mirror uses the same steps."""
    return next((f for n, f in CROWD_STEPS if same_side_open >= n), 1.0)


def position_risk(base, inst, same_side_open=0, rules=None):
    """Per-trade risk as a fraction of equity: base (the strategy's risk gene) × crowding × optional major-coin factor, then
    clamped to rules['max_risk'] (10 %). Used by the paper account. The OKX demo follow has its own caps."""
    rules = rules or ACCOUNT_RULES
    r = (base if base is not None else 0.0) * crowd_factor(same_side_open)
    if inst in rules.get('majors', ()):
        r *= rules.get('major_factor', 1.0)
    return min(r, rules.get('max_risk', MAX_TRADE_RISK))


def simulate_account(segs, series, start, end, equity=START_EQUITY, rules=ACCOUNT_RULES, curve_every=1, blocked=None):
    """ONE shared-equity account traded by several strategies at once, with cross (全仓) liquidation: the paper account of the champion team.

    segs: [dict(id, genes, from_i, wind_i=None, boosts=[(i0, i1 or None), ...])]. A segment takes signals on bars
    from_i <= i < wind_i (wind_i None = still seated); after wind_i its open positions run to their own exits.
    `boosts` are the bar ranges in which the segment is the team leader; a position opened on a leader signal keeps the
    leader size for its whole life.

    Same bar rules as simulate() (signal on a close, fill at the next open with slippage and fee, stop before target,
    gaps fill at the open, time exit, trailing stop, whole contract lots, funding). What is shared:
    - size = account equity × risk ÷ stop distance (risk = the strategy's risk gene for every seat, including the
      leader), then clamped to rules['max_risk'] (10 %) after crowding and the major-coin factor;
    - one position per coin for the whole account, at most MAX_POSITIONS per strategy;
    - margin = notional ÷ leverage (leverage = the gene, 1–20×, then the per-coin OKX cap;
      cap = rules['lever'] / 'leader_lever', both 20×), and a new entry needs it to fit into 95 % of the free margin
      (equity − margin in use); the entry is skipped when its stop, or every open stop at once, would leave mark equity
      at or below total maintenance margin;
    - a trade on one of rules['majors'] risks rules['major_factor'] times as much, still never above the 10 % cap;
    - `blocked` = {inst: [(i0, i1 or None)]}: bar ranges in which a coin takes no new entry (its OKX demo price was
      away from the real market, data.price_check); positions already open run on;
    - crowding: with 3+ positions already open on a side the next entry on that side risks half, with 6+ a quarter;
    - same-side cap: the open risk of every position on one side, including the leader, plus the new one stays within
      rules['same_side_cap'] × equity;
    - daily halt (days start at UTC+7 midnight): when the whole account, including the leader, has lost
      rules['daily_halt'] × the day's starting equity, every position is closed and no strategy opens until the next day;
    - liquidation is at the account level (cross / 全仓): when mark equity (cash + unrealized PnL, after fees and
      funding) is at or below total maintenance margin (sum of mark notional × MMR), every open position is closed
      at that bar's price with taker fees (reason `liquidation`). There is no partial or tiered liquidation and no
      separate reason `equity` — one breach closes the whole book. Checked after funding, after fills, at each bar's
      adverse extremes (lows for longs, highs for shorts, assumed simultaneous) and at the close;
    - funding is deducted from shared cash (notional × rate × side) and can itself trigger a cross liquidation;
    Returns dict(equity, curve, trades, positions, pending, pnl={seg id: realised + floating}, halted, metrics,
    liquidations = number of cross liquidations of the whole account).
    """
    insts = list(series)
    ts0 = series[insts[0]].ts
    ev = {s['id']: entries(s['genes'], series) for s in segs}
    by_id = {s['id']: s for s in segs}
    boosted = lambda s, i: any(a <= i and (b is None or i < b) for a, b in s.get('boosts') or ())
    cash, pos, trades, curve, pending = equity, {}, [], [], []
    liquidations = 0
    pnl = {s['id']: 0.0 for s in segs}
    peak, max_dd = equity, 0.0
    day, day_start, others_real, snap, halted = None, equity, 0.0, {}, False
    cap_side, halt = rules['same_side_cap'], rules['daily_halt']

    def close(inst, px, why, i, slip=True):
        nonlocal cash, others_real
        p = pos.pop(inst)
        fill, gain, net = close_proceeds(p, px, slip=slip)
        cash += gain
        pnl[p['seg']] += net
        others_real += gain - snap.pop(inst, 0.0)              # today's part: the result minus what it was worth at the day's start
        trades.append(dict(inst=inst, side=p['side'], entry=p['entry'], exit=fill, opened=p['opened'], closed=ts0[i], pnl=net,
                           funding=round(p['funding'], 4), qty=p['qty'], lever=p['lever'], reason=why, seg=p['seg'], boost=p['boost'],
                           liq=p.get('liq')))

    def liq_now(prices, i):
        """Full-account cross liquidation at `prices` (fees, no extra slip)."""
        nonlocal cash, liquidations
        if pos and book_liquidated(cash, pos, prices):
            liquidations += 1
            for inst in list(pos):
                close(inst, prices[inst], 'liquidation', i, slip=False)
            cash = max(cash, 0.0)                    # never below zero: the insurance fund takes a gap past bankruptcy
            return True
        return False

    for i in range(start, end):
        # 0) funding (shared cash; may itself trigger cross liquidation)
        for inst, p in pos.items():
            f = series[inst].funding
            if f:
                rate = f.get(ts0[i])
                if rate:
                    pay = p['qty'] * series[inst].o[i] * rate * p['side']
                    cash -= pay
                    p['funding'] = p.get('funding', 0.0) + pay
                    p['liq'] = liquidation_price(p['entry'], p['side'], p.get('lever'), mmr=p.get('mmr', maintenance_rate(inst)),
                                                 fee=FEE, margin=isolated_remaining_margin(p), qty=p['qty'])
        if liq_now(marks_at(series, list(pos), i, 'o'), i):
            pending = []
        # 1) fills at this open: the leader's signals first, then the segments in their order
        for sid, inst, d, dist, boost in sorted(pending, key=lambda x: not x[4]):
            s, g = series[inst], by_id[sid]['genes']
            if dist <= 0 or inst in pos or sum(1 for p in pos.values() if p['seg'] == sid) >= MAX_POSITIONS or halted:
                continue
            if blocked and any(a <= i and (b is None or i < b) for a, b in blocked.get(inst, ())):
                continue
            px = s.o[i] * (1 + SLIP * d)
            if dist / px < MIN_STOP_DIST:
                continue
            mark = cash + sum(p['qty'] * (series[k].o[i] - p['entry']) * p['side'] for k, p in pos.items())
            r = position_risk(g['risk'], inst, sum(1 for p in pos.values() if p['side'] == d), rules)
            qty = mark * r / dist
            if s.spec:
                ct, lot, mn = s.spec
                qty = math.floor(qty / ct / lot + 1e-9) * lot
                if qty <= 0 or qty < mn - 1e-12:
                    continue
                qty *= ct
            same = sum(p['qty'] * max((p['entry'] - p['stop']) * p['side'], 0) for p in pos.values() if p['side'] == d)
            if same + qty * dist > cap_side * mark * (1 + 1e-9):
                continue
            cap = rules['leader_lever'] if boost else rules['lever']
            mmr = maintenance_rate(inst)
            inst_cap = inst_max_leverage(inst, cap=cap)
            lever = clamp_leverage(g.get('leverage', DEFAULT_LEVERAGE), cap=inst_cap)
            margin = qty * px / lever
            if qty <= 0 or margin > (mark - sum(p['margin'] for p in pos.values())) * MARGIN_USE:
                continue
            rr = g['target_rr']
            new_p = dict(side=d, entry=px, qty=qty, stop=px - d * dist, target=px + d * rr * dist if rr > 0 else None, opened=ts0[i], bar=i,
                         fee=qty * px * FEE, dist=dist, lever=lever, margin=margin, funding=0.0, seg=sid, boost=boost, risk=r,
                         hold=min(g['max_hold'] * g.get('tf', 1), MAX_HOLD_H), trail=g.get('trail', 0),
                         liq=liquidation_price(px, d, lever, mmr=mmr), mmr=mmr)
            opens_now = marks_at(series, list(pos), i, 'o')
            ok, _why = cross_open_allowed(cash, pos, inst, new_p, opens_now)
            if not ok:
                continue
            cash -= new_p['fee']
            pos[inst] = new_p
        pending = []
        liq_now(marks_at(series, list(pos), i, 'o'), i)
        # 2) exits inside this bar (stops, targets, time — no per-position isolated liquidation)
        for inst in list(pos):
            p, s = pos[inst], series[inst]
            o, h, l, c = s.o[i], s.h[i], s.l[i], s.c[i]
            exit_px, why = None, None
            if p['side'] == 1:
                if o <= p['stop']: exit_px, why = o, 'stop'
                elif l <= p['stop']: exit_px, why = p['stop'], 'stop'
                elif p['target'] and o >= p['target']: exit_px, why = o, 'target'
                elif p['target'] and h >= p['target']: exit_px, why = p['target'], 'target'
            else:
                if o >= p['stop']: exit_px, why = o, 'stop'
                elif h >= p['stop']: exit_px, why = p['stop'], 'stop'
                elif p['target'] and o <= p['target']: exit_px, why = o, 'target'
                elif p['target'] and l <= p['target']: exit_px, why = p['target'], 'target'
            if exit_px is None and i - p['bar'] >= p['hold']:
                exit_px, why = c, 'time'
            if exit_px is not None:
                close(inst, exit_px, why, i)
            elif p['trail']:
                p['stop'] = max(p['stop'], h - p['dist']) if p['side'] == 1 else min(p['stop'], l + p['dist'])
        # 2b) conservative same-bar check: remaining positions at their adverse extremes together
        if pos:
            liq_now(adverse_prices(pos, series, i), i)
        # 3) mark at the close, cross-liquidation, daily halt, curve
        liq_now(marks_at(series, list(pos), i, 'c'), i)
        unreal = {k: p['qty'] * (series[k].c[i] - p['entry']) * p['side'] for k, p in pos.items()}
        eq = cash + sum(unreal.values())
        d_now = (ts0[i] + rules['day_shift']) // (24 * HOUR)
        if d_now != day:                                        # a new day starts with this bar: yesterday's close is the baseline
            day, halted, others_real = d_now, False, 0.0
            day_start = eq
            snap = {k: unreal[k] for k in pos}
        others_day = others_real + sum(unreal[k] - snap.get(k, 0.0) for k in pos)
        if not halted and day_start > 0 and others_day <= -halt * day_start:
            halted = True
            for inst in list(pos):
                close(inst, series[inst].c[i], 'daily_halt', i)
            eq = cash
        peak = max(peak, eq); max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0)
        if curve_every and (i - start) % curve_every == 0:
            curve.append((ts0[i], round(eq, 2)))
        # 4) signals on this close, from the seated segments
        for s in segs:
            if s['from_i'] <= i and (s.get('wind_i') is None or i < s['wind_i']):
                b = boosted(s, i)
                if halted:
                    continue
                for inst, d, dist in ev[s['id']].get(i, ()):
                    if inst not in pos:
                        pending.append((s['id'], inst, d, dist, b))
    last = end - 1
    for k, p in pos.items():
        pnl[p['seg']] += p['qty'] * (series[k].c[last] - p['entry']) * p['side'] - p['fee'] - p['funding']
    eq = cash + sum(p['qty'] * (series[k].c[last] - p['entry']) * p['side'] for k, p in pos.items())
    if curve_every and (not curve or curve[-1][0] != ts0[last]):
        curve.append((ts0[last], round(eq, 2)))
    wins = sum(1 for t in trades if t['pnl'] > 0)
    metrics = dict(ret=round((eq / equity - 1) * 100, 3), max_dd=round(max_dd * 100, 3), trades=len(trades),
                   win_rate=round(wins / len(trades) * 100, 1) if trades else 0.0)
    open_positions = [dict(inst=k, side=p['side'], entry=p['entry'], qty=p['qty'], stop=p['stop'], target=p['target'], opened=p['opened'],
                           lever=p['lever'], margin=round(p['margin'], 4), liq=p['liq'], funding=round(p['funding'], 4), seg=p['seg'], boost=p['boost'], risk=p['risk'])
                      for k, p in pos.items()]
    return dict(equity=eq, cash=cash, trades=trades, curve=curve, positions=open_positions, pending=pending, pnl=pnl, halted=halted, metrics=metrics, liquidations=liquidations)

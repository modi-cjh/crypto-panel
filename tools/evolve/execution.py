"""Shared execution: the ONE bar-by-bar engine used by both backtests and paper trading (同源执行).

Rules (identical in every caller):
- Signals are computed on a closed bar; the order fills at the NEXT bar's open, with slippage and a taker fee.
- One position per instrument and at most MAX_POSITIONS at once; size = equity × risk ÷ stop distance, notional capped at
  MAX_NOTIONAL × equity.
- Stop and target are checked inside each later bar; if a bar touches both, the stop is assumed first (conservative);
  a gap through the stop fills at the open. Positions also exit at the close after `max_hold` bars.
- Equity is marked to market at every close; max drawdown is measured on that curve.
- Timeframe gene `tf` (1 / 4 / 24 h): signals and the stop's ATR come from 4H / 1D bars built from the 1H bars (UTC
  aligned, complete bars only) and are decided when such a bar closes; fills, stops and targets still run on 1H bars.
- Gene `trail` = 1: after each bar the stop follows the best price at the original stop distance (never loosens).
"""
import math

FEE = .0005          # taker fee per side
SLIP = .0002         # slippage per fill
MAX_NOTIONAL = 3.0   # per position, × equity
START_EQUITY = 10000.0
MAX_HOLD_H = 720     # longest hold in hours, whatever the timeframe
MAX_POSITIONS = 3    # open positions per strategy at the same time; extra signals wait until one closes
HOUR = 3600_000


# ----------------------------------------------------------------- indicators (cached per series)
class Series:
    """One instrument's 1H candles (volume optional) with lazily cached indicators. `ref` is the BTC series on the
    same timeline (relative strength); the BTC series itself has no ref."""
    def __init__(self, inst, ts, o, h, l, c, v=None, ref=None):
        self.inst, self.ts, self.o, self.h, self.l, self.c = inst, ts, o, h, l, c
        self.v, self.ref = v, ref
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
            return self, list(range(self.n)), list(range(self.n))
        return self._get(('tf', hours), build)

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


# ----------------------------------------------------------------- the engine
def simulate(g, series, start, end, equity=START_EQUITY, close_at_end=False, curve_every=24):
    """Run genome g over bar indexes [start, end) of every series (all series share the same timeline).

    Returns dict(equity, trades, curve, positions, metrics). `positions` are still open at `end` unless close_at_end.
    """
    insts = list(series)
    cash, pos, trades, curve = equity, {}, [], []
    peak, max_dd, pending, dists = equity, 0.0, {}, {}
    rr, trail = g['target_rr'], g.get('trail', 0)
    hold = min(g['max_hold'] * g.get('tf', 1), MAX_HOLD_H)
    for i in range(start, end):
        # 1) fills at this bar's open for signals decided on the previous close
        for inst, d in pending.items():
            s = series[inst]
            dist = dists.get(inst, 0.0)
            if dist <= 0 or inst in pos or len(pos) >= MAX_POSITIONS:
                continue
            px = s.o[i] * (1 + SLIP * d)
            mark = cash + sum(p['qty'] * (series[k].o[i] - p['entry']) * p['side'] for k, p in pos.items())
            qty = min(mark * g['risk'] / dist, mark * MAX_NOTIONAL / px)
            if qty <= 0:
                continue
            cash -= qty * px * FEE
            pos[inst] = dict(side=d, entry=px, qty=qty, stop=px - d * dist, target=px + d * rr * dist if rr > 0 else None,
                             opened=s.ts[i], bar=i, fee=qty * px * FEE, dist=dist)
        pending, dists = {}, {}
        # 2) exits inside this bar
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
                exit_px *= (1 - SLIP * p['side'])
                fee = p['qty'] * exit_px * FEE
                pnl = p['qty'] * (exit_px - p['entry']) * p['side'] - fee
                cash += pnl
                trades.append(dict(inst=inst, side=p['side'], entry=p['entry'], exit=exit_px, opened=p['opened'], closed=s.ts[i],
                                   pnl=pnl - p['fee'], reason=why))
                del pos[inst]
            elif trail:                                     # after the bar: follow the best price, never loosen
                p['stop'] = max(p['stop'], h - p['dist']) if p['side'] == 1 else min(p['stop'], l + p['dist'])
        # 3) mark to market, drawdown, new signals on this close
        eq = cash + sum(p['qty'] * (series[k].c[i] - p['entry']) * p['side'] for k, p in pos.items())
        peak = max(peak, eq); max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0)
        if curve_every and (i - start) % curve_every == 0:
            curve.append((series[insts[0]].ts[i], round(eq, 2)))
        if i < end - 1 or not close_at_end:
            for inst in insts:
                if inst not in pos:
                    d = signal(g, series[inst], i)
                    if d:
                        pending[inst], dists[inst] = d, stop_distance(g, series[inst], i)
    last = end - 1
    eq = cash + sum(p['qty'] * (series[k].c[last] - p['entry']) * p['side'] for k, p in pos.items())
    if curve_every and (not curve or curve[-1][0] != series[insts[0]].ts[last]):
        curve.append((series[insts[0]].ts[last], round(eq, 2)))
    wins = sum(1 for t in trades if t['pnl'] > 0)
    gross_win = sum(t['pnl'] for t in trades if t['pnl'] > 0); gross_loss = -sum(t['pnl'] for t in trades if t['pnl'] < 0)
    metrics = dict(ret=round((eq / equity - 1) * 100, 3), max_dd=round(max_dd * 100, 3), trades=len(trades),
                   win_rate=round(wins / len(trades) * 100, 1) if trades else 0.0,
                   profit_factor=round(gross_win / gross_loss, 3) if gross_loss > 0 else (None if not trades else 99.0))
    open_positions = [dict(inst=k, side=p['side'], entry=p['entry'], qty=p['qty'], stop=p['stop'], target=p['target'], opened=p['opened'])
                      for k, p in pos.items()]
    return dict(equity=eq, cash=cash, trades=trades, curve=curve, positions=open_positions, metrics=metrics, pending=pending)


def fitness(m):
    """Training fitness: return per unit of drawdown (Calmar-like), damped when there are too few trades."""
    dd = max(m['max_dd'], 5.0)
    return round(m['ret'] / dd * min(1.0, m['trades'] / 30), 4)

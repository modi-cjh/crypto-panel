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
  (a gap past bankruptcy floors cash at 0; the difference is recorded as an insurance-fund adjustment, `insurance`,
  so start + Σ trade pnl + insurance = cash when flat).
  A new entry is skipped unless the account would reach every stop alive: with the new and every open stop reached
  (positions still open, full MM counted) equity less all stop fill costs must stay above total maintenance margin. Funding (`Series.funding`, {ts: rate}) is paid /
  received on every funding time a position is held: notional × rate (not margin × rate), longs pay positive rates, and
  is deducted from shared cash, so it can itself trigger a cross liquidation.
- Exits (2026-11-21 user, the same for every strategy — no per-strategy switch; see EXIT RULES below): a fixed stop
  AND a take-profit on every position, a reverse signal on the same coin closes it, and a trailing stop protects the
  profit. There is no time exit any more (MAX_HOLD_H / the max_hold gene only survive for the paper replay of bars
  before account['exit_rules_from']). Stop and target are checked inside each later bar; if a bar touches both, the
  stop is assumed first (conservative); a gap through the stop fills at the open. Same-bar order:
  open liquidation check → gap fills → adverse leg (each coin at its stop or adverse extreme, all together) liquidation
  check → stops → extremes check → targets → close liquidation check → trailing-stop update → reverse-signal closes
  (see `gap_exit`).
- Equity is marked to market at every close; max drawdown is measured on that curve.
- Timeframe gene `tf` (1 / 4 / 24 h): signals and the stop's ATR come from 4H / 1D bars built from the 1H bars (UTC
  aligned, complete bars only) and are decided when such a bar closes; fills, stops and targets still run on 1H bars.
- Gene `trail` is no longer used (kept in the genome so stored genomes / lineage stay valid): every position trails
  under the same rule (`trail_update`). Gene `max_hold` is no longer used either.
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
# Legacy time exit: only the paper replay of bars on or before account['exit_rules_from'] still closes a position after
# min(max_hold × tf, MAX_HOLD_H) hours. simulate() (evolution / backtests) and every later paper bar have no time exit.
MAX_HOLD_H = 720
MAX_POSITIONS = 3    # open positions per strategy at the same time; extra signals wait until one closes
HOUR = 3600_000

# ----------------------------------------------------------------- EXIT RULES (2026-11-21, every strategy, every position)
# 1) Fixed stop + take-profit: stop = entry ∓ dist, target = entry ± target_rr × dist, dist = stop_atr × ATR(14) of the
#    genome's timeframe at the signal bar. target_rr is a gene in [1, 6] (genome.GENES; 0 = "no target" is gone, a stored
#    0 maps to DEFAULT_TARGET_RR = 3R in genome.fix()). target_rr(g) below applies the same mapping to any dict.
# 2) Reverse signal: a signal on the same coin in the other direction, decided on a bar's close, closes the position at
#    that close (reason 'reverse_signal'); the opposite entry may then fill at the next open like any other signal.
#    Whose signal counts is an ACCOUNT-level rule, ACCOUNT_RULES['reverse_close']:
#      'own'  = only the strategy that opened the position (default; a strategy's own exit, so it does not depend on who
#               else is seated and the backtest of one strategy (simulate) is exactly the same rule);
#      'team' = any seated team member (candidate; to be compared out of sample before switching).
# 3) Trailing stop (TRAIL_START_R / TRAIL_DIST_R): after every 1H close, best = highest high (long) / lowest low (short)
#    since the fill; once best has moved TRAIL_START_R × dist in favour, stop = max(stop, best − TRAIL_DIST_R × dist)
#    for a long (min(stop, best + TRAIL_DIST_R × dist) for a short) — it only moves in the position's favour. With 1R / 1R
#    the stop reaches breakeven exactly when the trade is 1R up, then follows the best price 1R behind. The `trail` gene
#    no longer switches this on or off. A stop hit after the trail activated is recorded as 'trailing_stop'.
#    Granularity: paper / backtests update the trailing stop bar by bar on 1H candles; the live / demo follower
#    (tools/okx_demo_trader.py, runs hourly at :50 UTC) recomputes it from OKX's closed 1H candles since its entry and
#    amends the exchange stop once per run, so on the exchange the stop can lag the paper stop by up to an hour.
# 4) No time exit (MAX_HOLD_H and max_hold are legacy, see above).
DEFAULT_TARGET_RR = 3.0   # target for genomes that had none (target_rr 0): 3R, see genome.fix()
TARGET_RR_RANGE = (1.0, 6.0)
TRAIL_DIST_R = 1.0        # then the stop follows the best price at this many stop distances
REVERSE_CLOSE = ('own', 'team')

# ----------------------------------------------------------------- TRAIL & RISK RULES (2026-11-21, claude/trail-champion-risk)
# One implementation for the evolve backtest (simulate), the paper account (simulate_account) and the OKX follow
# (tools/okx_demo_trader.py, live and demo share it). Chosen by the out-of-sample replay in tools/analysis/trail_risk_replay.py
# (evidence: /workspace/modi/trail-risk-2026-11-21/SUMMARY.md).
# a) Trailing stop / breakeven (trail_stop_level): with `unit` = the position's stop distance (R),
#      favourable move >= BREAKEVEN_R × unit  → stop = max(stop, entry) for a long (min for a short): breakeven;
#      favourable move >= TRAIL_START_R × unit → stop = best ∓ TRAIL_DIST_R × unit (only in the position's favour).
#    BREAKEVEN_R None = no breakeven step. Either step sets the position's `trailing` flag (a later stop hit is recorded
#    as 'trailing_stop': the stop was moved by the protection rule).
#    R on the OKX follow = trail_unit(paper dist, live dist) = min of the two (the smaller unit reaches 1R first and
#    protects the position that was actually opened: the live entry fills at a different price than the paper one, so
#    the same paper stop is a different distance from it; motivating case ETH 2026-11-21: 1.01R by the live entry/stop,
#    0.99R by the paper dist, the trail never started). Paper and backtests have only one distance (their own).
# b) Same-champion same-bar cap (entry_risk_factor / same_bar_groups): entries of ONE seat (paper segment / backtest
#    genome) decided on the SAME signal bar (the 1H close they were decided on; they all fill at the next open) in the
#    SAME direction share one position's risk: each gets its normal risk ÷ n. n = that group's signals that are eligible
#    when the fills start (coin not already held, not blocked, positive stop distance), at most the seat's free position
#    slots (MAX_POSITIONS − its open positions). Different bar, direction or seat = different groups.
# c) New / winding seat half risk: an entry gets × NEW_SEAT_FACTOR when its seat is winding (lost its seat; fill bar >=
#    wind_i), or when it is the seat's FIRST entry bar (all entries of that bar) and the seat has been held for less than
#    NEW_SEAT_HOURS at the fill (tenure = from the seat's start; consecutive segments of the same champion count as one
#    seat). Only seats exist in the paper account, so (c) is a paper / OKX-follow rule; simulate() (a single genome's
#    fitness backtest, no seat) applies (a) and (b) only.
#    The factors multiply the normal per-trade risk AFTER the 5–10 % band, crowding, major doubling and the 10 % cap:
#    the result may be below 5 % (intended; the band bounds the gene, these rules only ever make a trade smaller).
#    The paper publishes the factor of every position in signals.json (`risk_factor`); the OKX follow multiplies its own
#    risk by it (missing = 1, older signals). Paper: these rules only apply after account['trail_risk_from'] (bars / fills
#    on or before it keep PRIOR_TRAIL and no factor), so already closed history replays unchanged.
# Chosen 2026-11-21 by the OOS replay (2025-06-03 → 2025-11-30; SUMMARY.md): 0.5R had the best return / max drawdown for
# the static team, the seat-churn team and the single-genome fitness engine. A 0.5R breakeven step was better only on the
# 7-day recent sample and worse in the churn OOS, so it stays off. With TRAIL_DIST_R = 1 a 0.5R start first halves the
# loss (stop = entry ∓ 0.5R), reaches breakeven at 1R and then trails 1R behind the best price.
TRAIL_START_R = .5        # trailing starts once the best price is this many stop distances in profit (was 1.0, PRIOR_TRAIL)
BREAKEVEN_R = None        # breakeven step: None = off
PRIOR_TRAIL = dict(start_r=1.0, dist_r=1.0, be_r=None)    # PR #110 trail (bars on or before account['trail_risk_from']); never edit
TRAIL = dict(start_r=TRAIL_START_R, dist_r=TRAIL_DIST_R, be_r=BREAKEVEN_R)
SAME_BAR_SHARE = True     # (b) on
NEW_SEAT_HOURS = 24       # (c) window; None = off. 24 h beat 48 h in the churn OOS (return / drawdown, worst day, ≥ 25 % days)
NEW_SEAT_FACTOR = .5
PRIOR_RISK = dict(same_bar_share=False, new_seat_h=None)   # sizing before account['trail_risk_from']; never edit


def trail_unit(paper_dist, live_dist=None):
    """R for the trailing stop / breakeven: the smaller positive one of the paper stop distance and the live one
    (|live entry − live initial stop|); only the paper one when the live one is missing (older state entries)."""
    vals = [float(d) for d in (paper_dist, live_dist) if d is not None and float(d) > 0]
    return min(vals) if vals else 0.0


def trail_stop_level(side, entry, best, stop, unit, trail=None):
    """(new stop, protected) under TRAIL & RISK RULES (a): breakeven step, then trailing; only in the position's favour.
    `trail` = dict(start_r, dist_r, be_r), default TRAIL. protected = a step is active (the `trailing` flag)."""
    t = TRAIL if trail is None else trail
    new, on = stop, False
    if unit and unit > 0:
        fav = (best - entry) * side
        be = t.get('be_r')
        if be is not None and fav >= be * unit - 1e-12:
            if (entry - new) * side > 0:
                new = entry
            on = True
        if fav >= t['start_r'] * unit - 1e-12:
            x = best - side * t['dist_r'] * unit
            if (x - new) * side > 0:
                new = x
            on = True
    return new, on


def entry_risk_factor(group_n=1, seat_age_h=None, first=True, winding=False, rules=None):
    """Multiplier of an entry's normal risk (TRAIL & RISK RULES b, c): 1 / group_n when rules['same_bar_share'] and the
    seat has n > 1 same-bar same-direction entries; × rules['new_seat_factor'] when the seat is winding, or this is its
    first entry bar and it has been seated less than rules['new_seat_h'] hours (None = off). Never above 1."""
    rules = ACCOUNT_RULES if rules is None else rules
    f = 1.0
    if rules.get('same_bar_share', SAME_BAR_SHARE) and group_n and group_n > 1:
        f /= group_n
    h = rules.get('new_seat_h', NEW_SEAT_HOURS)
    if h is not None and (winding or (first and seat_age_h is not None and seat_age_h < h)):
        f *= rules.get('new_seat_factor', NEW_SEAT_FACTOR)
    return min(1.0, f)


def same_bar_groups(pending, held, held_by_seat, eligible=None):
    """{(seat, side): n} for entries decided on one bar (TRAIL & RISK RULES b). pending = [(seat, inst, side, dist, ...)];
    held = coins already open on the account; held_by_seat = {seat: open positions}; eligible(inst) -> bool for other
    entry blocks (e.g. price_off). `pending` must be in fill order: a coin signalled by several seats counts only for the
    first. n is capped at the seat's free slots (MAX_POSITIONS − open). n counts eligible SIGNALS, not fills: an entry
    later skipped for margin / lot size / the liquidation gate still counts, so the group may use less than one
    position's risk (conservative), never more."""
    out, claimed = {}, set()
    for x in pending:                 # in fill order: a coin goes to the first seat that signals it (one per account)
        sid, inst, d, dist = x[0], x[1], x[2], x[3]
        if dist and dist > 0 and inst not in held and inst not in claimed and (eligible is None or eligible(inst)):
            claimed.add(inst)
            out[(sid, d)] = out.get((sid, d), 0) + 1
    return {k: min(n, max(0, MAX_POSITIONS - held_by_seat.get(k[0], 0))) for k, n in out.items()}


def live_same_bar_groups(pending, held, real_by_seat, virtual_by_seat, blocked):
    """PRICE_OFF LIVE: {(seat, side): n} of TRAIL & RISK RULES b for the OKX LIVE follower, which opens BOTH the paper
    account's entries and the virtual price_off ones (simulate_account off_book_from; paper.update price_off_live).
    The paper's own groups (same_bar_groups with price_off coins not eligible) are unchanged and still size the paper
    account; this one counts, on one fill bar, every entry the paper account OR the virtual book takes as a signal, so
    the live entries of one seat / bar / side share one position's risk together (1 ordinary + 1 price_off = ½ + ½).
    pending = [(seat, inst, side, dist, ...)] in fill order; held = coins open in the paper account OR the virtual book
    (the live follower holds one position per coin); real_by_seat / virtual_by_seat = the seat's open paper / virtual
    positions; blocked(inst) -> bool = refused by price_off on this fill. Walked in fill order with the SAME slot tests
    as the fills: an ordinary entry needs real < MAX_POSITIONS (the paper account ignores the virtual book), a virtual one
    real + virtual < MAX_POSITIONS; each accepted entry takes its slot and claims its coin. Like same_bar_groups, n counts
    SIGNALS: entries later skipped for margin / lot / MIN_STOP_DIST / max_same_side still count, so the live group may use
    less than one position's risk (conservative), never more. Never below the paper's n for the ordinary entries."""
    out, claimed = {}, set()
    real, virt = dict(real_by_seat), dict(virtual_by_seat)
    for x in pending:
        sid, inst, d, dist = x[0], x[1], x[2], x[3]
        if not (dist and dist > 0) or inst in held or inst in claimed:
            continue
        r, v = real.get(sid, 0), virt.get(sid, 0)
        if blocked(inst):
            if r + v >= MAX_POSITIONS:
                continue
            virt[sid] = v + 1
        else:
            if r >= MAX_POSITIONS:
                continue
            real[sid] = r + 1
        claimed.add(inst)
        out[(sid, d)] = out.get((sid, d), 0) + 1
    return out


def target_rr(g):
    """The take-profit multiple every position uses: the gene kept inside TARGET_RR_RANGE; a missing / < .5 gene (the
    old 'no target') is DEFAULT_TARGET_RR. Same mapping as genome.fix()."""
    rr = g.get('target_rr')
    if rr is None or rr < .5:
        return DEFAULT_TARGET_RR
    return min(TARGET_RR_RANGE[1], max(TARGET_RR_RANGE[0], float(rr)))


def trail_update(p, h, l, trail=None):
    """Update p['best'] with this bar's extreme and move the stop under the trailing / breakeven rule (trail_stop_level,
    only in the position's favour; `trail` default TRAIL). Sets p['trailing'] once a step has activated. Returns the stop."""
    d, dist = p['side'], p['dist']
    p['best'] = max(p.get('best', p['entry']), h) if d == 1 else min(p.get('best', p['entry']), l)
    new, on = trail_stop_level(d, p['entry'], p['best'], p['stop'], dist, trail)
    p['stop'] = new
    if on:
        p['trailing'] = True
    return p['stop']


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


def signal_sets(g, series):
    """(entries, reversals) of genome g on every series, from one pass over the species signals.
    entries   = {1H index i: [(inst, d, stop distance)]} in series order: equivalent to calling signal() and
                stop_distance() on every bar (direction gene and volatility filter applied);
    reversals = {1H index i: {inst: d}}: every bar where the species fires, BEFORE the direction gene and the volatility
                filter. These close an opposite position (EXIT RULES: reverse signal) — the filters only gate entries,
                so a long-only or regime-filtered strategy still gets the same reverse exit as every other strategy."""
    tf, out, rev = g.get('tf', 1), {}, {}
    want = {'long': 1, 'short': -1}.get(g['direction'])
    lo, hi, k = g['vol_min'], g['vol_max'], g['stop_atr']
    for inst, s in series.items():
        x = s.tf(tf)[0]
        cidx, vr, atr = s.close_index(tf), s.vol_ratio(), x.atr()
        for j, d in _raw_signals(g, x):
            i = cidx[j]
            if i < 2:
                continue
            rev.setdefault(i, {})[inst] = d
            if want and d != want or vr[i] < lo or vr[i] > hi:
                continue
            out.setdefault(i, []).append((inst, d, k * atr[j]))
    return out, rev


def entries(g, series):
    """All entry signals of genome g on every series, as {1H index i: [(inst, d, stop distance)]} (see signal_sets)."""
    return signal_sets(g, series)[0]


def reversals(g, series):
    """{1H index i: {inst: d}}: the species' raw signals, used only for reverse-signal exits (see signal_sets)."""
    return signal_sets(g, series)[1]


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
    `extra(inst, p)` may add fields (for example `seg` / `boost` on the shared account). Returns (cash, insurance):
    the new cash balance and the insurance-fund compensation that brought a negative balance back to 0. The trades keep
    the full loss, so start + Σ trade pnl + Σ insurance = cash once the book is flat.
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
    insurance = max(-cash, 0.0)
    return cash + insurance, insurance


def _stop_corner(cash, pos, fixed, inst, new_p, prices):
    """Worst point of the path to the stops, *before* any stop fills: every stopped position at its stop price, fixed
    ones at the mark, all still open. Returns (equity, total MM, cost of filling every stop)."""
    book = dict(pos)
    book.update(fixed)
    book[inst] = new_p
    px = dict(prices)
    px[inst] = new_p['entry']
    stopped = dict(pos, **{inst: new_p})
    for k, p in stopped.items():
        px[k] = p['stop']
    cost = 0.0
    for k, p in stopped.items():
        fill, gain, _ = close_proceeds(p, p['stop'], slip=True)
        cost += p['qty'] * (p['stop'] - fill) * p['side'] + p['qty'] * fill * FEE    # exit slippage + exit fee
    return book_equity(cash, book, px), book_mm(book, px), cost


def cross_open_allowed(cash, pos, inst, new_p, prices, fixed=None):
    """Whether `new_p` may be opened on this cross-margin book.

    `cash` is the balance *before* the new entry fee, without the unrealized PnL of `pos` / `fixed`; `new_p['fee']` is
    the entry fee. `prices` marks every key of `pos` and `fixed` (typically the bar's open / the last price).
    `fixed` = positions that share the account but have no stop this program manages (manual positions on the OKX demo
    account): they stay at the mark in every scenario and keep their maintenance margin. Returns (ok, reason) with
    reason None on success, otherwise `equity_below_mm` or `stop_before_liquidation`.

    Rules (strict inequalities):
    1. After the entry, mark equity must stay above total MM including the new position.
    2. The account must reach every stop alive: with the new position and every open managed position at its stop
       price — still open, so the whole book's maintenance margin counts — and fixed ones at the mark, equity minus the
       slippage and fees of filling all those stops must stay above that total MM. Equity − MM only shrinks as a price
       moves toward its stop, so this corner bounds every path in which stops are reached one by one or together
       (including the new position's stop alone); subtracting every fill cost while still counting every position's MM
       also bounds each intermediate state while the stops are being executed. Positions left after all stops are the
       fixed ones, so their MM — or cash > 0 for an empty book — is covered too.
    Gaps through a stop are not covered (no rule can guarantee those). Leverage is not reduced to satisfy these rules —
    the entry is skipped.
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
    eq, mm, cost = _stop_corner(cash_after, pos, fixed, inst, new_p, prices)
    if eq - cost <= mm or eq - cost <= 0:
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


# Same-bar event order (both engines). OHLC does not say how several coins moved inside one bar, so one conservative
# assumption is used everywhere: every coin first moves against its position, all together, then towards it.
#   a) at the open: account liquidation check, then gaps through a stop / target fill at the open;
#   b) adverse leg, before any stop fills: each position at its stop if the bar reaches it, else at its adverse extreme
#      (`adverse_path_prices`) — an account liquidation here closes the whole book and no later exit of this bar runs;
#   c) stops fill at the stop (stop before target, as before), then the positions left are checked at their extremes;
#   d) favourable leg: targets fill;
#   e) at the close: account liquidation check, then time / switch exits at the close.
# A position that reaches its stop is valued at the stop, not the extreme beyond it, so a stop that comes first is not
# turned into a liquidation; but a time exit at the close, or a target, can no longer hide an intrabar liquidation.
def gap_exit(p, o):
    """(price, reason) when the bar opens through the stop or the target, else None."""
    if (o - p['stop']) * p['side'] <= 0:
        return o, 'stop'
    if p['target'] and (o - p['target']) * p['side'] >= 0:
        return o, 'target'
    return None


def stop_hit(p, h, l):
    return l <= p['stop'] if p['side'] == 1 else h >= p['stop']


def target_hit(p, h, l):
    return bool(p['target']) and (h >= p['target'] if p['side'] == 1 else l <= p['target'])


def adverse_path_prices(pos, series, i):
    """Worst same-bar prices before any stop fills: the adverse extreme, but no further than the stop."""
    out = {}
    for inst, p in pos.items():
        s = series[inst]
        out[inst] = max(s.l[i], p['stop']) if p['side'] == 1 else min(s.h[i], p['stop'])
    return out


# ----------------------------------------------------------------- the engine
def simulate(g, series, start, end, equity=START_EQUITY, close_at_end=False, curve_every=24, events=None, stop_entries_at=None,
             risk=None, lever_cap=None, extra_equity=None, reverse=None, rules=None):
    """Run genome g over bar indexes [start, end) of every series (all series share the same timeline).
    `events` = entries(g, series) and `reverse` = reversals(g, series), when the caller already computed them (signal_sets).
    Exits: the EXIT RULES above (stop + target, trailing stop, the genome's own reverse signals; no time exit).
    `stop_entries_at`: from this bar index on no new signals are taken; open positions run to their own exits
    (a member that lost its seat winds down instead of being closed at market).
    `risk` / `lever_cap` override the genome's risk gene and leverage gene (still kept inside MIN_TRADE_RISK–MAX_TRADE_RISK
    and capped by LEVERAGE),
    and `extra_equity` ({ts: USDT}, carried forward) is equity outside this run that sizing and the margin check may
    count on (legacy slot accounting in paper.py).

    `rules` (default ACCOUNT_RULES): only 'trail' (dict(start_r, dist_r, be_r), default TRAIL) and 'same_bar_share' are
    read here (TRAIL & RISK RULES a, b: same-bar same-direction entries of this genome share one position's risk). The
    seat rule (c) does not apply to a single-genome backtest.

    Returns dict(equity, trades, curve, positions, metrics). `positions` are still open at `end` unless close_at_end.
    """
    rules = ACCOUNT_RULES if rules is None else rules
    trail = rules.get('trail', TRAIL)
    insts = list(series)
    ts0 = series[insts[0]].ts
    if events is None or reverse is None:
        ev2, rv2 = signal_sets(g, series)
        events = ev2 if events is None else events
        reverse = rv2 if reverse is None else reverse
    cash, pos, trades, curve = equity, {}, [], []
    peak, max_dd, pending, dists = equity, 0.0, {}, {}
    rr = target_rr(g)
    risk = clamp_risk(g.get('risk') if risk is None else risk)
    desired_lever = LEVERAGE if lever_cap is None else lever_cap
    if lever_cap is None:
        desired_lever = g.get('leverage', DEFAULT_LEVERAGE)
    cap, extra = min(int(desired_lever), LEVERAGE), 0.0
    liquidations, insurance = 0, 0.0
    for i in range(start, end):
        if extra_equity is not None:
            extra = extra_equity.get(ts0[i], extra)
        def _liq_now(prices):
            nonlocal cash, liquidations, insurance
            if pos and book_liquidated(cash + extra, pos, prices):
                cash, paid = liquidate_book(cash, pos, prices, ts0[i], trades)
                liquidations += 1
                insurance += paid
                return True
            return False

        def _exit(inst, px, why):
            nonlocal cash
            p, s = pos.pop(inst), series[inst]
            fill, gain, net = close_proceeds(p, px, slip=True)
            cash += gain
            trades.append(dict(inst=inst, side=p['side'], entry=p['entry'], exit=fill, opened=p['opened'], closed=s.ts[i],
                               pnl=net, funding=round(p['funding'], 4), qty=p['qty'], lever=p['lever'], reason=why,
                               liq=p.get('liq')))
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
        groups = same_bar_groups([(None, k, d, dists.get(k, 0.0)) for k, d in pending.items()], pos, {None: len(pos)})
        for inst, d in pending.items():
            s = series[inst]
            dist = dists.get(inst, 0.0)
            if dist <= 0 or inst in pos or len(pos) >= MAX_POSITIONS:
                continue
            px = s.o[i] * (1 + SLIP * d)
            if dist / px < MIN_STOP_DIST:
                continue
            mark = cash + sum(p['qty'] * (series[k].o[i] - p['entry']) * p['side'] for k, p in pos.items()) + extra
            qty = mark * risk * entry_risk_factor(groups.get((None, d), 1), rules=dict(same_bar_share=rules.get('same_bar_share', SAME_BAR_SHARE), new_seat_h=None)) / dist
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
            new_p = dict(side=d, entry=px, qty=qty, stop=px - d * dist, target=px + d * rr * dist,
                         opened=s.ts[i], bar=i, best=px, fee=qty * px * FEE, dist=dist, lever=lever, margin=margin,
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
        # 2) exits inside this bar, in the same-bar event order above (no per-position isolated liquidation)
        for inst in list(pos):                                       # a) gaps through a stop / target fill at the open
            hit = gap_exit(pos[inst], series[inst].o[i])
            if hit:
                _exit(inst, *hit)
        if pos and not _liq_now(adverse_path_prices(pos, series, i)):   # b) adverse leg, before any stop fills
            for inst in list(pos):                                   # c) stops, then the rest at their extremes
                if stop_hit(pos[inst], series[inst].h[i], series[inst].l[i]):
                    _exit(inst, pos[inst]['stop'], 'trailing_stop' if pos[inst].get('trailing') else 'stop')
            if pos and not _liq_now(adverse_prices(pos, series, i)):
                for inst in list(pos):                               # d) targets
                    if target_hit(pos[inst], series[inst].h[i], series[inst].l[i]):
                        _exit(inst, pos[inst]['target'], 'target')
        # 3) at the close: cross-liquidation check first, then switch exits, trailing stops, reverse signals, mark to market
        closes = marks_at(series, list(pos), i, 'c')
        _liq_now(closes)
        for inst in list(pos):
            p, s = pos[inst], series[inst]
            if close_at_end and i == end - 1:
                _exit(inst, s.c[i], 'switch')
            else:                                           # after the bar: trail the best price, never loosen
                trail_update(p, s.h[i], s.l[i], trail)
        for inst, d in reverse.get(i, {}).items():          # a reverse signal on this close closes the position at the close
            if inst in pos and pos[inst]['side'] == -d:
                _exit(inst, series[inst].c[i], 'reverse_signal')
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
                           lever=p['lever'], margin=round(p['margin'], 4), liq=p['liq'], funding=round(p['funding'], 4), dist=p['dist'],
                           best=p.get('best'), trailing=bool(p.get('trailing')))
                      for k, p in pos.items()]
    return dict(equity=eq, cash=cash, trades=trades, curve=curve, positions=open_positions, metrics=metrics, pending=pending,
                liquidations=liquidations, insurance=insurance)


def fitness(m):
    """Training fitness: return per unit of drawdown (Calmar-like), damped when there are too few trades."""
    dd = max(m['max_dd'], 5.0)
    return round(m['ret'] / dd * min(1.0, m['trades'] / 30), 4)


# ----------------------------------------------------------------- the shared (cross-margin) team account
# major_factor: a trade on one of MAJORS risks this many times the usual amount (the most liquid coins, and the ones
# whose OKX demo prices follow the real market). The same MAJORS list sets the 0.5 % MMR tier.
# Per-trade risk never exceeds 10 % of equity, for every seat including the leader (the gene itself is 5–10 %; a
# missing or out-of-range gene is kept inside 5–10 %).
# Leverage is chosen by the gene (1–20×) and then clamped to that coin's OKX max; it only sets initial margin.
# A new entry is skipped when its stop (or every open stop at once) would leave mark equity at or below total MM.
# major_factor still doubles a major-coin entry, then the 10 % cap is applied so a doubled 8 % gene becomes 10 %, not 16 %.
# leader_risk / leader_lever are the same caps: the leader uses its own risk and leverage genes, not a fixed bigger size.
# 2026-11-20 (PR #107): 5–20 % / cap 20 % / crowd half at 10 (was 2–10 % / cap 10 % / half at 3, quarter at 6).
# 2026-11-20 (later): lowered to 5–10 % / cap 10 % / crowd still half at 10, the same values as the OKX follow
# (tools/okx_demo_trader.py, demo and live).
MAX_TRADE_RISK = .10
MIN_TRADE_RISK = .05
# Same-direction position cap (lightweight): at most this many open positions on one side (long or short), the leader
# included; a new entry on a full side is skipped, the other side and every exit are untouched.
# Why a count, not notional or margin: the account sizes by risk, so notional is risk ÷ stop distance and swings 10×
# with the stop width, and margin also depends on leverage, so neither tracks how many separate bets are on one
# side. The risk-based same_side_cap (20 %) did not bind either, because crowding shrinks every extra entry.
# A position count is the same number in the paper account and on the OKX demo mirror (no ctVal, avgPx or leverage
# differences), and it is what crowd_factor already counts.
# The first cap was 6 (the old CROWD_STEPS quarter step); the user has since raised it to 20. With CROWD_STEPS
# ((10, .5),) entries 11–20 on one side risk half.
MAX_SAME_SIDE_POSITIONS = 20   # 2026-11-18 user: at most 20 per side (was 15, before that 10, before that 6)
ACCOUNT_RULES = dict(leader_risk=MAX_TRADE_RISK, leader_lever=LEVERAGE, lever=LEVERAGE, same_side_cap=None, daily_halt=.25,   # 2026-11-15 user: no same-side total-risk cap, daily halt 25 %
                     reverse_close='own',   # 2026-11-21: whose reverse signal closes a position, 'own' | 'team' (EXIT RULES above)
                     day_shift=7 * HOUR, majors=MAJORS, major_factor=2.0, max_risk=MAX_TRADE_RISK, min_risk=MIN_TRADE_RISK,
                     min_lever=MIN_LEVERAGE, margin_mode=MARGIN_MODE, max_same_side=MAX_SAME_SIDE_POSITIONS,
                     # TRAIL & RISK RULES (above): trail / breakeven, same-bar share, new / winding seat half risk
                     trail=TRAIL, same_bar_share=SAME_BAR_SHARE, new_seat_h=NEW_SEAT_HOURS, new_seat_factor=NEW_SEAT_FACTOR)
CROWD_STEPS = ((10, .5),)     # same-side positions already open → risk factor for the next entry: <10 full, ≥10 half (2026-11-20; was ((6, .25), (3, .5)))
# Paper-account sizing eras. simulate_account() replays the account from its start every run, so fills that already
# happened keep the sizing of their era and positions that were filled (open ones included) keep their original size:
# - LEGACY_SIZING: fills on or before `risk_from` (account['risk_rules_from'] in paper.py, the PR #107 stamp):
#   2–10 %, cap 10 %, half at 3 / quarter at 6;
# - PRIOR_SIZING: fills after `risk_from` and on or before `risk_cap_from` (account['risk_cap_from'], the first run with
#   the 5–10 % band): the PR #107 rules, 5–20 %, cap 20 %, half at 10;
# - after `risk_cap_from`: the current ACCOUNT_RULES (5–10 %, cap 10 %, half at 10).
# Do not change these dicts' values: they describe fills that already happened.
LEGACY_SIZING = dict(min_risk=.02, max_risk=.10, crowd_steps=((6, .25), (3, .5)))
PRIOR_SIZING = dict(min_risk=.05, max_risk=.20, crowd_steps=((10, .5),))


def same_side_full(same_side_open, cap=MAX_SAME_SIDE_POSITIONS):
    """True when `same_side_open` positions already open on one side leave no room for another on that side.
    cap None or <= 0 switches the check off. The paper account and the OKX demo mirror both call this."""
    return bool(cap) and cap > 0 and same_side_open >= cap


def crowd_factor(same_side_open, steps=None):
    """Many positions on one side are one bet: with 10 or more already open on that side a new entry risks half
    (everyone, the leader included). The OKX mirror uses the same steps. `steps` defaults to CROWD_STEPS."""
    return next((f for n, f in (CROWD_STEPS if steps is None else steps) if same_side_open >= n), 1.0)


def clamp_risk(base, lo=MIN_TRADE_RISK, hi=MAX_TRADE_RISK):
    """A strategy's base risk kept inside [lo, hi]; missing = lo (5 %), the same as the OKX follow's RISK_RANGE."""
    return lo if base is None else min(hi, max(lo, float(base)))


def position_risk(base, inst, same_side_open=0, rules=None):
    """Per-trade risk as a fraction of equity: base (the strategy's risk gene, kept inside rules['min_risk']–
    rules['max_risk'], 5–10 %; missing = 5 %) × crowding (rules['crowd_steps'], default CROWD_STEPS) × optional major-coin factor, then
    clamped to rules['max_risk'] (10 %). Used by the paper account; the OKX follow applies the same steps itself."""
    rules = rules or ACCOUNT_RULES
    lo, hi = rules.get('min_risk', MIN_TRADE_RISK), rules.get('max_risk', MAX_TRADE_RISK)
    r = clamp_risk(base, lo, hi) * crowd_factor(same_side_open, rules.get('crowd_steps'))
    if inst in rules.get('majors', ()):
        r *= rules.get('major_factor', 1.0)
    return min(r, hi)


def simulate_account(segs, series, start, end, equity=START_EQUITY, rules=ACCOUNT_RULES, curve_every=1, blocked=None, side_cap_from=None,
                     risk_from=None, risk_cap_from=None, exit_from=None, manual=None, trail_risk_from=None, off_book_from=None):
    """ONE shared-equity account traded by several strategies at once, with cross (全仓) liquidation: the paper account of the champion team.

    segs: [dict(id, genes, from_i, wind_i=None, boosts=[(i0, i1 or None), ...])]. A segment takes signals on bars
    from_i <= i < wind_i (wind_i None = still seated); after wind_i its open positions run to their own exits.
    `boosts` are the bar ranges in which the segment is the team leader; a position opened on a leader signal keeps the
    leader size for its whole life.

    Same bar rules as simulate() (signal on a close, fill at the next open with slippage and fee, stop before target,
    gaps fill at the open, the EXIT RULES above, whole contract lots, funding). What is shared:
    - exit eras, so the replay never rewrites trades that already closed: exit_from = a bar index
      (account['exit_rules_from'] in paper.py). On bars up to and including it the OLD exits apply: target only when the
      seat's stored gene had one (`old_exit` = the gene values as stored, target_rr < .5 = none), trailing only for
      trail = 1 (immediately, at the stop distance), a time exit after min(max_hold × tf, MAX_HOLD_H) hours, no reverse
      close. On every later bar the NEW exits apply to every position, including ones opened earlier: a position without
      a target gets entry ± target_rr × dist (the gene, 0 mapped to DEFAULT_TARGET_RR) at the open of the first new bar,
      trailing under TRAIL_START_R / TRAIL_DIST_R, reverse-signal closes (rules['reverse_close']), no time exit.
      None = new exits on every bar;
    - size = account equity × risk ÷ stop distance (risk = the strategy's risk gene for every seat, including the
      leader, kept inside rules['min_risk']–rules['max_risk'] = 5–10 %), then clamped to rules['max_risk'] (10 %) after
      crowding and the major-coin factor. Sizing eras, so the replay never resizes positions that were already filled:
      risk_from = a bar index: fills on or before it are sized with LEGACY_SIZING (2–10 %, cap 10 %, half at 3 / quarter
      at 6) and the segment's `old_risk` (its stored gene, when given); risk_cap_from = a later bar index: fills after
      risk_from (or from the start when risk_from is None) and on or before it are sized with PRIOR_SIZING (5–20 %,
      cap 20 %, half at 10) and `old_risk`; later fills use `rules` (None = that era is skipped);
    - one position per coin for the whole account, at most MAX_POSITIONS per strategy;
    - margin = notional ÷ leverage (leverage = the gene, 1–20×, then the per-coin OKX cap;
      cap = rules['lever'] / 'leader_lever', both 20×), and a new entry needs it to fit into 95 % of the free margin
      (equity − margin in use); the entry is skipped when its stop, or every open stop at once, would leave mark equity
      at or below total maintenance margin;
    - a trade on one of rules['majors'] risks rules['major_factor'] times as much, still never above the 10 % cap;
    - `blocked` = {inst: [(i0, i1 or None)]}: bar ranges in which a coin takes no new entry (its OKX demo price was
      away from the real market, data.price_check); positions already open run on. The live follower does not lose
      these signals: see PRICE_OFF LIVE below;
    - crowding (rules['crowd_steps'], default CROWD_STEPS): with 10+ positions already open on a side the next entry on that side risks half;
    - same-side cap: when rules['same_side_cap'] is set, the open risk of one side plus the new entry stays within
      that × equity; None (the default since 2026-11-15) = no total-risk cap per side, only the count below;
    - same-side count: with rules['max_same_side'] positions already open on a side (the leader included), a new
      entry on that side is skipped (same_side_full); the other side and exits are not affected. side_cap_from = a
      bar index: the count only applies to fills after that bar, so switching it on does not rewrite the replay of
      positions that were already open (None = every bar);
    - daily halt (days start at UTC+7 midnight): when the whole account, including the leader, has lost
      rules['daily_halt'] × the day's starting equity, every position is closed and no strategy opens until the next day;
    - liquidation is at the account level (cross / 全仓): when mark equity (cash + unrealized PnL, after fees and
      funding) is at or below total maintenance margin (sum of mark notional × MMR), every open position is closed
      at that bar's price with taker fees (reason `liquidation`). There is no partial or tiered liquidation and no
      separate reason `equity` — one breach closes the whole book. Checked after funding, after fills, on the adverse leg
      before any stop fills (each coin at its stop or adverse extreme, assumed simultaneous), after the stops at the
      extremes, and at the close before time exits (same-bar order: see `gap_exit`); a gap past bankruptcy floors cash
      at 0 and records the difference as an insurance adjustment;
    - funding is deducted from shared cash (notional × rate × side) and can itself trigger a cross liquidation;
    - manual closes (reason `manual_user`; paper.MANUAL_CLOSES): `manual` = {(inst, opened ts): (at ts, price, from_i)}.
      Only the open position of exactly that coin AND that entry time is closed, at `price` (the real fill: taker fee,
      no extra slip), at the OPEN of the first bar k with ts[k] >= at and k > from_i, before that bar's funding and
      fills. from_i (None = no limit) is an era stamp like exit_from: the last bar already published when the close was
      added (paper account['manual_from']), so the replay of bars that were already published never changes; a close
      whose time lies inside them is booked at the first new bar. The trade's `closed` is `at` (the real time), `booked`
      the bar it was booked on. It is booked like any other close (cash, pnl, daily halt, curve), so from bar k on the
      coin may take a new entry like any coin and the position no longer counts for the same-side count / crowding /
      margin. Nothing happens when that position is not open at bar k (it already left by its own exit);
    - TRAIL & RISK RULES (see the block above; account['trail_risk_from'] in paper.py): trail_risk_from = a bar index.
      Bars on or before it trail with PRIOR_TRAIL (1R / 1R, no breakeven) and fills on or before it get no risk factor
      (PRIOR_RISK), so the replay of what was already published does not change. Later bars trail every open position
      with rules['trail'] (default TRAIL), and later fills get entry_risk_factor(): the seat's same-bar same-direction
      group (same_bar_groups over that bar's pending entries) shares one position's risk (rules['same_bar_share']), and
      a winding seat (fill bar >= wind_i), or the first entry bar of a seat seated less than rules['new_seat_h'] hours
      (seg 'tenure_ts' = the seat's start in ms, default ts[from_i]; seg 'chain' = the seat key shared by consecutive
      segments of one champion, default the id; its first fill is tracked over the whole replay) risks
      × rules['new_seat_factor']. The factor multiplies the risk after the band / crowding / majors / 10 % cap (it may go
      below 5 %), and is kept as the position's `risk_factor`. None = the new rules on every bar;
    - PRICE_OFF LIVE (off_book_from, 2026-11-25): None (default) = off, nothing below runs and the result is unchanged.
      A bar index = entries filled after that bar that are refused ONLY because their coin is in `blocked` (they passed
      dist > 0, coin not held, the seat's MAX_POSITIONS, not halted, i.e. every check before the price_off one) are
      tracked as VIRTUAL positions: they never touch cash, pos, pnl, the curve, the daily halt, liquidation, crowding or
      any count of the real account, so the paper account is byte-identical with or without them. A virtual entry
      needs its coin free of another virtual position, the seat's real + virtual positions < MAX_POSITIONS and the
      fill's stop distance >= MIN_STOP_DIST; it fills at the open with slippage like a real one, gets
      stop = entry ∓ dist, target = entry ± target_rr(genes) × dist (always the current EXIT RULES), and exits by the
      same price rules as a real position: gap through stop / target at the open, stop ('stop' / 'trailing_stop'),
      target, then at the close the trailing stop (rules['trail'] after trail_risk_from) and the reverse signal
      (rules['reverse_close']). The live follower opens the paper entries AND these, so their same-bar groups are
      counted together (live_same_bar_groups: paper + virtual entries of the seat / bar / side, coins held by either
      book excluded, the same slot tests as the fills): a virtual entry's risk_factor is entry_risk_factor() of that
      combined group, and a paper entry filled on such a bar whose combined group is larger than its paper group gets
      `live_risk_factor` (<= its risk_factor) besides; the paper sizing (risk, qty, risk_factor) never changes. E.g. one
      paper + one price_off entry of a seat on one bar: paper risk_factor 1 (the account takes only one), live 0.5 +
      0.5. They are published for the OKX LIVE follower only (paper.update → signals.json price_off_live, and
      live_risk_factor on the paper signals); returned as off_book (open) and off_book_trades (closed), only when
      off_book_from is not None.
    Returns dict(equity, curve, trades, positions, pending, pnl={seg id: realised + floating}, halted, metrics,
    liquidations = number of cross liquidations of the whole account, insurance = total insurance-fund compensation,
    adjustments = [dict(ts, kind='insurance', amount)]). Trades and pnl keep the full loss, so
    equity == start + Σ pnl + insurance.
    """
    insts = list(series)
    ts0 = series[insts[0]].ts
    sets = {s['id']: signal_sets(s['genes'], series) for s in segs}
    ev = {k: v[0] for k, v in sets.items()}
    rev = {k: v[1] for k, v in sets.items()}
    by_id = {s['id']: s for s in segs}
    mode = rules.get('reverse_close', 'own')
    if mode not in REVERSE_CLOSE:
        raise ValueError(f"reverse_close must be one of {REVERSE_CLOSE}, not {mode!r}")
    old_era = lambda i: exit_from is not None and i <= exit_from     # bars that keep the exits they were traded with

    def old_exit(sid):
        """The seat's exit genes as stored (before the 2026-11-21 rules): target_rr (< .5 = none), trail, max_hold, tf."""
        g, o = by_id[sid]['genes'], by_id[sid].get('old_exit') or {}
        return dict(target_rr=o.get('target_rr', g['target_rr']), trail=o.get('trail', g.get('trail', 0)),
                    max_hold=o.get('max_hold', g['max_hold']), tf=o.get('tf', g.get('tf', 1)))
    upgraded = exit_from is None
    new_era = lambda i: trail_risk_from is None or i > trail_risk_from     # TRAIL & RISK RULES apply
    trail_new = rules.get('trail', TRAIL)
    risk_new = dict(same_bar_share=rules.get('same_bar_share', SAME_BAR_SHARE), new_seat_h=rules.get('new_seat_h', NEW_SEAT_HOURS),
                    new_seat_factor=rules.get('new_seat_factor', NEW_SEAT_FACTOR))
    first_fill = {}     # {seat chain: bar of its first fill} (TRAIL & RISK RULES c)
    track = off_book_from is not None        # PRICE_OFF LIVE: virtual positions, see the docstring; never read by the account
    vpos, vtrades = {}, []

    def vclose(inst, px, why, i):
        p = vpos.pop(inst)
        vtrades.append(dict(inst=inst, side=p['side'], entry=p['entry'], exit=px, opened=p['opened'], closed=ts0[i], reason=why,
                            seg=p['seg'], boost=p['boost'], dist=p['dist'], risk_factor=p['risk_factor']))
    boosted = lambda s, i: any(a <= i and (b is None or i < b) for a, b in s.get('boosts') or ())
    cash, pos, trades, curve, pending = equity, {}, [], [], []
    liquidations, insurance, adjustments = 0, 0.0, []
    pnl = {s['id']: 0.0 for s in segs}
    peak, max_dd = equity, 0.0
    day, day_start, others_real, snap, halted = None, equity, 0.0, {}, False
    cap_side, halt = rules['same_side_cap'], rules['daily_halt']

    def close(inst, px, why, i, slip=True, closed=None, **extra):
        nonlocal cash, others_real
        p = pos.pop(inst)
        fill, gain, net = close_proceeds(p, px, slip=slip)
        cash += gain
        pnl[p['seg']] += net
        others_real += gain - snap.pop(inst, 0.0)              # today's part: the result minus what it was worth at the day's start
        trades.append(dict(inst=inst, side=p['side'], entry=p['entry'], exit=fill, opened=p['opened'], closed=ts0[i] if closed is None else closed,
                           pnl=net, funding=round(p['funding'], 4), qty=p['qty'], lever=p['lever'], reason=why, seg=p['seg'], boost=p['boost'],
                           liq=p.get('liq'), **({'risk_factor': p['risk_factor']} if p.get('risk_factor', 1.0) != 1.0 else {}), **extra))

    manual_at = {}      # {bar index k: [(inst, opened, at, price)]}: the bar whose open books each manual close
    for (m_inst, m_opened), (m_at, m_px, m_from) in (manual or {}).items():
        lo = max(start, m_from + 1) if m_from is not None else start
        k = next((j for j in range(lo, end) if ts0[j] >= m_at), None)
        if k is not None:
            manual_at.setdefault(k, []).append((m_inst, int(m_opened), int(m_at), float(m_px)))

    def liq_now(prices, i):
        """Full-account cross liquidation at `prices` (fees, no extra slip)."""
        nonlocal cash, liquidations, insurance
        if pos and book_liquidated(cash, pos, prices):
            liquidations += 1
            for inst in list(pos):
                close(inst, prices[inst], 'liquidation', i, slip=False)
            if cash < 0:                             # never below zero: the insurance fund takes a gap past bankruptcy
                paid = -cash                         # recorded as an account adjustment; trades and members keep the full loss
                insurance += paid
                adjustments.append(dict(ts=ts0[i], kind='insurance', amount=paid))
                cash = 0.0
            return True
        return False

    for i in range(start, end):
        if not upgraded and not old_era(i):
            # first bar under the new exits: every open position gets a take-profit (gaps / hits from this bar on)
            upgraded = True
            for p in pos.values():
                if not p['target']:
                    p['target'] = p['entry'] + p['side'] * target_rr(by_id[p['seg']]['genes']) * p['dist']
        # manual closes (the user's, reason manual_user): only that exact position, at its real price, before this bar
        for m_inst, m_opened, m_at, m_px in manual_at.get(i, ()):
            if m_inst in pos and pos[m_inst]['opened'] == m_opened:
                close(m_inst, m_px, 'manual_user', i, slip=False, closed=m_at, booked=ts0[i])
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
        is_blocked = lambda k: bool(blocked) and any(a <= i and (b is None or i < b) for a, b in blocked.get(k, ()))
        groups = same_bar_groups(sorted(pending, key=lambda x: not x[4]), pos, {sid: sum(1 for p in pos.values() if p['seg'] == sid) for sid in by_id},
                                 eligible=lambda k: not is_blocked(k)) if new_era(i) and not halted else {}
        # PRICE_OFF LIVE: the live follower's same-bar groups (paper + virtual entries together, live_same_bar_groups);
        # they size the virtual entries and give the paper entries of this bar their `live_risk_factor` (paper sizing
        # keeps `groups` above)
        live_on = track and i > off_book_from and new_era(i) and not halted
        vgroups = live_same_bar_groups(sorted(pending, key=lambda x: not x[4]), set(pos) | set(vpos),
                                       {sid: sum(1 for p in pos.values() if p['seg'] == sid) for sid in by_id},
                                       {sid: sum(1 for p in vpos.values() if p['seg'] == sid) for sid in by_id}, is_blocked) if live_on else {}

        def off_book_entry(sid, inst, d, dist, boost):
            """PRICE_OFF LIVE: a signal refused only by price_off becomes a virtual position (never touches the account)."""
            g = by_id[sid]['genes']
            if inst in vpos or sum(1 for p in pos.values() if p['seg'] == sid) + sum(1 for p in vpos.values() if p['seg'] == sid) >= MAX_POSITIONS:
                return
            px = series[inst].o[i] * (1 + SLIP * d)
            if dist / px < MIN_STOP_DIST:
                return
            factor = 1.0
            if new_era(i):
                seg = by_id[sid]
                fb = first_fill.get(seg.get('chain', sid))
                factor = entry_risk_factor(vgroups.get((sid, d), 1), seat_age_h=(ts0[i] - seg.get('tenure_ts', ts0[seg['from_i']])) / HOUR,
                                           first=fb is None or fb == i,
                                           winding=seg.get('wind_i') is not None and i >= seg['wind_i'], rules=risk_new)
            vpos[inst] = dict(side=d, entry=px, stop=px - d * dist, target=px + d * target_rr(g) * dist, opened=ts0[i], bar=i, dist=dist,
                              seg=sid, boost=boost, best=px, risk_factor=factor)
        for sid, inst, d, dist, boost in sorted(pending, key=lambda x: not x[4]):
            s, g = series[inst], by_id[sid]['genes']
            if dist <= 0 or inst in pos or sum(1 for p in pos.values() if p['seg'] == sid) >= MAX_POSITIONS or halted:
                continue
            if blocked and any(a <= i and (b is None or i < b) for a, b in blocked.get(inst, ())):
                if track and i > off_book_from:
                    off_book_entry(sid, inst, d, dist, boost)
                continue
            if (side_cap_from is None or i > side_cap_from) and same_side_full(sum(1 for p in pos.values() if p['side'] == d), rules.get('max_same_side')):
                continue
            px = s.o[i] * (1 + SLIP * d)
            if dist / px < MIN_STOP_DIST:
                continue
            mark = cash + sum(p['qty'] * (series[k].o[i] - p['entry']) * p['side'] for k, p in pos.items())
            n_same = sum(1 for p in pos.values() if p['side'] == d)
            if risk_from is not None and i <= risk_from:      # filled before the PR #107 sizing change: original size
                r = position_risk(by_id[sid].get('old_risk', g['risk']), inst, n_same, {**rules, **LEGACY_SIZING})
            elif risk_cap_from is not None and i <= risk_cap_from:   # filled under the 5–20 % rules: original size
                r = position_risk(by_id[sid].get('old_risk', g['risk']), inst, n_same, {**rules, **PRIOR_SIZING})
            else:
                r = position_risk(g['risk'], inst, n_same, rules)
            factor = live_factor = 1.0
            if new_era(i):                                    # TRAIL & RISK RULES b, c
                seg = by_id[sid]
                chain = seg.get('chain', sid)
                fb = first_fill.get(chain)
                age_h, first = (ts0[i] - seg.get('tenure_ts', ts0[seg['from_i']])) / HOUR, fb is None or fb == i
                wind = seg.get('wind_i') is not None and i >= seg['wind_i']
                factor = entry_risk_factor(groups.get((sid, d), 1), seat_age_h=age_h, first=first, winding=wind, rules=risk_new)
                r *= factor
                live_factor = factor
                if live_on:                                   # PRICE_OFF LIVE: the live follower's share (never sizes the paper)
                    live_factor = min(factor, entry_risk_factor(vgroups.get((sid, d), 1), seat_age_h=age_h, first=first, winding=wind, rules=risk_new))
            qty = mark * r / dist
            if s.spec:
                ct, lot, mn = s.spec
                qty = math.floor(qty / ct / lot + 1e-9) * lot
                if qty <= 0 or qty < mn - 1e-12:
                    continue
                qty *= ct
            same = sum(p['qty'] * max((p['entry'] - p['stop']) * p['side'], 0) for p in pos.values() if p['side'] == d)
            if cap_side is not None and same + qty * dist > cap_side * mark * (1 + 1e-9):
                continue
            cap = rules['leader_lever'] if boost else rules['lever']
            mmr = maintenance_rate(inst)
            inst_cap = inst_max_leverage(inst, cap=cap)
            lever = clamp_leverage(g.get('leverage', DEFAULT_LEVERAGE), cap=inst_cap)
            margin = qty * px / lever
            if qty <= 0 or margin > (mark - sum(p['margin'] for p in pos.values())) * MARGIN_USE:
                continue
            if old_era(i):                                    # filled under the old exits: its target / trail / time exit as then
                o = old_exit(sid)
                rr = o['target_rr'] if o['target_rr'] >= .5 else 0
                old = dict(hold=min(o['max_hold'] * o['tf'], MAX_HOLD_H), trail=o['trail'])
            else:
                rr, old = target_rr(g), dict(hold=None, trail=0)
            new_p = dict(side=d, entry=px, qty=qty, stop=px - d * dist, target=px + d * rr * dist if rr > 0 else None, opened=ts0[i], bar=i,
                         fee=qty * px * FEE, dist=dist, lever=lever, margin=margin, funding=0.0, seg=sid, boost=boost, risk=r, best=px,
                         liq=liquidation_price(px, d, lever, mmr=mmr), mmr=mmr, risk_factor=factor, **old)
            if live_factor != factor:                         # PRICE_OFF LIVE only (off_book_from); never read by the account
                new_p['live_risk_factor'] = live_factor
            opens_now = marks_at(series, list(pos), i, 'o')
            ok, _why = cross_open_allowed(cash, pos, inst, new_p, opens_now)
            if not ok:
                continue
            cash -= new_p['fee']
            pos[inst] = new_p
            first_fill.setdefault(by_id[sid].get('chain', sid), i)
        pending = []
        liq_now(marks_at(series, list(pos), i, 'o'), i)
        # 2) exits inside this bar, in the same-bar event order (see gap_exit); no per-position isolated liquidation
        for inst in list(pos):                                       # a) gaps through a stop / target fill at the open
            hit = gap_exit(pos[inst], series[inst].o[i])
            if hit:
                close(inst, hit[0], hit[1], i)
        if pos and not liq_now(adverse_path_prices(pos, series, i), i):   # b) adverse leg, before any stop fills
            for inst in list(pos):                                   # c) stops, then the rest at their extremes
                if stop_hit(pos[inst], series[inst].h[i], series[inst].l[i]):
                    close(inst, pos[inst]['stop'], 'trailing_stop' if pos[inst].get('trailing') else 'stop', i)
            if pos and not liq_now(adverse_prices(pos, series, i), i):
                for inst in list(pos):                               # d) targets
                    if target_hit(pos[inst], series[inst].h[i], series[inst].l[i]):
                        close(inst, pos[inst]['target'], 'target', i)
        if track:                                                    # PRICE_OFF LIVE: virtual exits, same price rules (a, c, d)
            for inst in list(vpos):
                p, s = vpos[inst], series[inst]
                hit = gap_exit(p, s.o[i])
                if hit:
                    vclose(inst, hit[0], hit[1], i)
                elif stop_hit(p, s.h[i], s.l[i]):
                    vclose(inst, p['stop'], 'trailing_stop' if p.get('trailing') else 'stop', i)
                elif target_hit(p, s.h[i], s.l[i]):
                    vclose(inst, p['target'], 'target', i)
        # 3) at the close: cross-liquidation check first, then trailing stops and reverse signals (old era: time exits and
        #    the trail gene instead); daily halt, curve
        liq_now(marks_at(series, list(pos), i, 'c'), i)
        if old_era(i):
            for inst in list(pos):
                p, s = pos[inst], series[inst]
                p['best'] = max(p['best'], s.h[i]) if p['side'] == 1 else min(p['best'], s.l[i])
                if p['hold'] is not None and i - p['bar'] >= p['hold']:
                    close(inst, s.c[i], 'time', i)
                elif p['trail']:
                    p['stop'] = max(p['stop'], s.h[i] - p['dist']) if p['side'] == 1 else min(p['stop'], s.l[i] + p['dist'])
        else:
            trail_now = trail_new if new_era(i) else PRIOR_TRAIL
            for inst in list(pos):
                trail_update(pos[inst], series[inst].h[i], series[inst].l[i], trail_now)
            seated = [x['id'] for x in segs if x['from_i'] <= i and (x.get('wind_i') is None or i < x['wind_i'])] if mode == 'team' else []
            for inst in list(pos):
                p = pos[inst]
                who = [p['seg']] if mode == 'own' else [p['seg']] + seated
                if any(rev[w].get(i, {}).get(inst) == -p['side'] for w in who):
                    close(inst, series[inst].c[i], 'reverse_signal', i)
        if track:                                                    # PRICE_OFF LIVE: virtual trailing stop, then reverse signal
            trail_v = trail_new if new_era(i) else PRIOR_TRAIL
            seated_v = [x['id'] for x in segs if x['from_i'] <= i and (x.get('wind_i') is None or i < x['wind_i'])] if mode == 'team' else []
            for inst in list(vpos):
                p = vpos[inst]
                trail_update(p, series[inst].h[i], series[inst].l[i], trail_v)
                who = [p['seg']] if mode == 'own' else [p['seg']] + seated_v
                if any(rev[w].get(i, {}).get(inst) == -p['side'] for w in who):
                    vclose(inst, series[inst].c[i], 'reverse_signal', i)
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
                           lever=p['lever'], margin=round(p['margin'], 4), liq=p['liq'], funding=round(p['funding'], 4), seg=p['seg'], boost=p['boost'], risk=p['risk'],
                           dist=p['dist'], best=p['best'], trailing=bool(p.get('trailing')), risk_factor=p.get('risk_factor', 1.0),
                           **({'live_risk_factor': p['live_risk_factor']} if 'live_risk_factor' in p else {}))
                      for k, p in pos.items()]
    out = dict(equity=eq, cash=cash, trades=trades, curve=curve, positions=open_positions, pending=pending, pnl=pnl, halted=halted, metrics=metrics, liquidations=liquidations,
               insurance=insurance, adjustments=adjustments, first_fill=first_fill)
    if track:
        out['off_book'] = [dict(inst=k, side=p['side'], entry=p['entry'], stop=p['stop'], target=p['target'], opened=p['opened'], seg=p['seg'],
                                boost=p['boost'], dist=p['dist'], best=p['best'], trailing=bool(p.get('trailing')), risk_factor=p['risk_factor'])
                           for k, p in vpos.items()]
        out['off_book_trades'] = vtrades
    return out

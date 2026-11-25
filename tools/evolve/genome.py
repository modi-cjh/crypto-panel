"""Strategy genome: which signal family ("species") it uses and the parameters it runs with.

Every genome carries every gene; genes a species does not use are inert but still inherited, so crossover can
produce hybrids and a mutation of `species` can wake them up.
"""
import random

SPECIES = ['momentum', 'mean_revert', 'vol_breakout', 'trend', 'vol_surge', 'rel_strength']
SPECIES_NAMES = {'momentum': '动量突破', 'mean_revert': '均值回归', 'vol_breakout': '波动突破', 'trend': '均线趋势',
                 'vol_surge': '放量异动', 'rel_strength': '相对强弱'}
# name: (kind, low, high)   kinds: int / float / choice
GENES = {
    'species': ('choice', SPECIES, None),
    'direction': ('choice', ['both', 'long', 'short'], None),
    'tf': ('choice', [1, 4, 24], None),  # timeframe in hours: signals and ATR on 1H / 4H / 1D bars (4H, 1D built from 1H)
    'lookback': ('int', 10, 200),        # momentum channel / mean-reversion / volume-average / relative-strength window (bars)
    'threshold': ('float', .8, 3.5),     # mean-reversion z-score
    'expand': ('float', 1.2, 4.0),       # volatility-breakout bar range ÷ ATR
    'fast': ('int', 5, 60),              # trend fast EMA
    'slow': ('int', 30, 240),            # trend slow EMA
    'vol_mult': ('float', 1.5, 6.0),     # volume surge: volume > vol_mult × average of the previous `lookback` bars
    'rs_th': ('float', .02, .3),         # relative strength: (coin − BTC) return over `lookback` bars crosses ±rs_th
    'stop_atr': ('float', .8, 5.0),      # stop distance in ATR(14)
    'target_rr': ('float', 1.0, 6.0),    # take-profit = entry ± target_rr × stop distance; every position has one (2026-11-21:
                                         # was 0–6 with < .5 = no target; fix() maps a stored 0 to DEFAULT_TARGET_RR = 3R)
    'max_hold': ('int', 4, 240),         # NOT USED since 2026-11-21 (no time exit); kept so stored genomes / lineage stay valid
    'trail': ('choice', [0, 1], None),   # NOT USED since 2026-11-21: every position trails (execution.trail_update); kept for stored genomes
    'risk': ('float', .05, .10),         # equity at risk per trade, chosen by the strategy itself (5–10 %; 5–20 % in PR #107, 2–10 % before)
    'leverage': ('int', 1, 20),          # leverage the strategy wants (sets initial margin on cross); clamped to the coin's OKX max
    'vol_min': ('float', .3, 1.2),       # trade only when ATR% ÷ its baseline is inside [vol_min, vol_max]
    'vol_max': ('float', 1.0, 4.0),
}
DEFAULT_TARGET_RR = 3.0   # = execution.DEFAULT_TARGET_RR (kept here so genome stays import-free)
DECIMALS = {'vol_mult': 2, 'rs_th': 3, 'threshold': 2, 'expand': 2, 'stop_atr': 2, 'target_rr': 2, 'risk': 4, 'vol_min': 2, 'vol_max': 2}


TF_NAMES = {1: '1 小时', 4: '4 小时', 24: '日线'}
# Genomes saved before a gene existed get these defaults (1H, no trailing, 10× leverage — the old cap).
DEFAULTS = {'tf': 1, 'trail': 0, 'vol_mult': 3.0, 'rs_th': .1, 'leverage': 10}


def fix(g):
    """Clamp to bounds, keep slow > fast, vol_max > vol_min, and treat tiny targets as 'no target'.
    Genomes saved before a gene existed get its neutral default (1H, no trailing, 10× leverage), so they
    behave as before except out-of-range risk/leverage is clamped into the current 5–10 % / 1–20× bands (a gene saved
    under the old 2–10 % band below 5 % becomes 5 %; one saved under the 5–20 % band above 10 % becomes 10 %).
    A missing risk gene becomes 5 %.
    target_rr (2026-11-21, every position has a take-profit unless it would be ≤ 0, execution.take_profit): a missing gene or one below .5 — the old 'no target', in
    practice a stored 0 — becomes DEFAULT_TARGET_RR (3R); .5–1 becomes 1R (the new lower bound); above 6 becomes 6R.
    New genomes never reach the mapping: random_genome draws inside [1, 6] and mutate() clamps its step to the range."""
    if g.get('risk') is None:
        g['risk'] = GENES['risk'][1]
    if g.get('target_rr') is None or g['target_rr'] < .5:
        g['target_rr'] = DEFAULT_TARGET_RR
    for k, v in DEFAULTS.items():
        g.setdefault(k, v)
    for k, (kind, lo, hi) in GENES.items():
        if kind == 'int':
            g[k] = int(round(min(hi, max(lo, g[k]))))
        elif kind == 'float':
            g[k] = round(min(hi, max(lo, g[k])), DECIMALS.get(k, 3))
    if g['slow'] <= g['fast'] + 5:
        g['slow'] = min(GENES['slow'][2], g['fast'] + 6)
    if g['vol_max'] <= g['vol_min'] + .2:
        g['vol_max'] = round(min(GENES['vol_max'][2], g['vol_min'] + .3), 2)
    return g


def random_genome(rng, species=None):
    g = {}
    for k, (kind, lo, hi) in GENES.items():
        g[k] = rng.choice(lo) if kind == 'choice' else rng.randint(lo, hi) if kind == 'int' else rng.uniform(lo, hi)
    if species:
        g['species'] = species
    return fix(g)


def crossover(a, b, rng):
    """Uniform crossover: each gene from either parent."""
    a, b = fix(dict(a)), fix(dict(b))          # parents saved before a gene existed get its default first
    return fix({k: (a if rng.random() < .5 else b)[k] for k in GENES})


def mutate(g, rng, rate=.25, scale=.2):
    """Each gene mutates with probability `rate`: numbers by a gaussian step of `scale` × range, choices by re-draw.
    The species gene mutates at a third of the rate so families stay recognisable across generations."""
    g = fix(dict(g))
    for k, (kind, lo, hi) in GENES.items():
        if rng.random() >= (rate / 3 if k == 'species' else rate):
            continue
        if kind == 'choice':
            g[k] = rng.choice(lo)
        else:
            g[k] = min(hi, max(lo, g[k] + rng.gauss(0, scale * (hi - lo))))   # clamp here, so a step below 1R is 1R, not the 3R default
    return fix(g)


def describe(g):
    """One-line human description (Chinese), used by the dashboard. Genomes saved before a gene existed get its default."""
    g = {**DEFAULTS, **g}
    sp = g['species']
    core = {'momentum': f"突破前 {g['lookback']} 根高低点", 'mean_revert': f"偏离 {g['lookback']} 根均值 {g['threshold']}σ 反向",
            'vol_breakout': f"单根振幅 > {g['expand']}×ATR 顺势", 'trend': f"EMA{g['fast']} 穿越 EMA{g['slow']}",
            'vol_surge': f"成交量 > 前 {g['lookback']} 根均量 {g['vol_mult']} 倍 顺 K 线方向",
            'rel_strength': f"{g['lookback']} 根涨幅比 BTC 强 / 弱 {g['rs_th'] * 100:.1f}%"}[sp]
    side = {'both': '多空', 'long': '只做多', 'short': '只做空'}[g['direction']]
    rr = g.get('target_rr')
    tgt = f"止盈 {rr if rr is not None and rr >= .5 else DEFAULT_TARGET_RR}R"
    tf = TF_NAMES[g.get('tf', 1)]
    return (f"{SPECIES_NAMES[sp]} · {tf} · {core} · {side} · 止损 {g['stop_atr']}×ATR · {tgt} · 移动止损 · 反向信号平仓 · "
            f"风险 {g['risk'] * 100:.1f}% · 杠杆 {int(g.get('leverage', 10))}×")

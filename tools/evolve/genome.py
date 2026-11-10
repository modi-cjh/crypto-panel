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
    'target_rr': ('float', 0.0, 6.0),    # target as multiple of stop distance; < .5 means no target
    'max_hold': ('int', 4, 240),         # bars of the genome's timeframe (capped at MAX_HOLD_H hours)
    'trail': ('choice', [0, 1], None),   # 1: the stop trails the best price at the stop distance after every bar
    'risk': ('float', .003, .02),        # equity at risk per trade
    'vol_min': ('float', .3, 1.2),       # trade only when ATR% ÷ its baseline is inside [vol_min, vol_max]
    'vol_max': ('float', 1.0, 4.0),
}
DECIMALS = {'vol_mult': 2, 'rs_th': 3, 'threshold': 2, 'expand': 2, 'stop_atr': 2, 'target_rr': 2, 'risk': 4, 'vol_min': 2, 'vol_max': 2}


TF_NAMES = {1: '1 小时', 4: '4 小时', 24: '日线'}
DEFAULTS = {'tf': 1, 'trail': 0, 'vol_mult': 3.0, 'rs_th': .1}


def fix(g):
    """Clamp to bounds, keep slow > fast, vol_max > vol_min, and treat tiny targets as 'no target'.
    Genomes saved before a gene existed get its neutral default (1H, no trailing), so they behave exactly as before."""
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
    if g['target_rr'] < .5:
        g['target_rr'] = 0.0
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
            g[k] = g[k] + rng.gauss(0, scale * (hi - lo))
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
    tgt = f"止盈 {g['target_rr']}R" if g['target_rr'] else '无止盈'
    tf = TF_NAMES[g.get('tf', 1)]
    stop = '移动止损' if g.get('trail') else '止损'
    return (f"{SPECIES_NAMES[sp]} · {tf} · {core} · {side} · {stop} {g['stop_atr']}×ATR · {tgt} · "
            f"最长 {g['max_hold']} 根 · 风险 {g['risk'] * 100:.1f}%")

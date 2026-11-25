"""MARGIN SIZING (2026-11-25, user: 「保证金是本金的2%-10%，乘杠杆就是仓位大小」, 「按新规则改，不加单笔止损上限」).

A new entry's size used to come from its stop: risk = r × equity, qty = risk ÷ stop distance (r = the risk gene, 5–10 %,
majors × 2, crowding × 0.5, capped at 10 %, then × risk_factor). From the cutover on it comes from margin:

    m        = MARGIN_RANGE[0] + (clamp(risk gene, GENE_BAND) − 5 %) / 5 % × 8 %     (5 % → 2 %, 7.5 % → 6 %, 10 % → 10 %)
    m        = clamp(m × crowding (≥ 10 same-side open → × 0.5) × majors (× 2), MARGIN_RANGE) × risk_factor
    margin   = base equity × m
    notional = margin × leverage      (leverage unchanged: gene 1–20×, then the coin's OKX max)
    qty      = notional ÷ entry price, rounded down to the lot (below minSz: no entry)

The stop no longer sizes the position: a stop-out loses base × m × leverage × stop distance. There is NO per-trade stop-loss
cap (user decision). risk_factor (same-bar share, new / winding seat half, live_risk_factor of PRICE_OFF LIVE) multiplies
AFTER the clamp and may take m below 2 %, exactly as it may take today's risk below 5 % (scheme B of the analysis in
/workspace/modi/sizing-2026-11-25/SUMMARY.md). Every other rule is unchanged: 95 % margin use, cross_open_allowed, the
same-side count of 20, the 25 % daily halt, minSz, leverage cap 20.

Base equity:
- paper / backtests: the account's own mark equity (cash + unrealised PnL of its own positions), as before;
- OKX follower: live_base_equity() = exchange USDT equity minus the unrealised PnL of every position that is not one of the
  program's own open mirrors (manual positions and OKX_ADOPT adopted ones), in both signs, floored at 0.

Where it applies (one cutover, the same bar for paper and live):
- paper (execution.simulate_account(margin_from=…)): fills on bars after account['margin_sizing_from'] (paper.CUTOVER_KEYS,
  stamped once by cutover() on the NEXT-HOUR bar C of the first GitHub run with this code); fills on or before it keep
  STOP_RISK_SIZING (the frozen stop-risk rules). Filled positions are never resized.
- signals.json: every signal whose fill time `opened` is after that stamp carries sizing='margin' (mark_margin()); the OKX
  follower sizes by margin only those, and by the old stop risk every other one (so a follower deployed before the
  cutover, or a replay of a state that is not stamped yet, trades exactly as before).
- simulate() (the evolution's single-genome fitness backtest): margin sizing on every bar from the first run with this
  code (rules['sizing'], default SIZING) — fitness is a backtest with no published history, it scores each genome under
  the rule it will be traded with.
Pure functions only (no execution / OKX imports) so the paper engine and the OKX follower share them.
"""
import math

MARGIN_SIZING = 'margin'
STOP_RISK = 'stop_risk'
SIZING = MARGIN_SIZING                 # simulate()'s default (rules['sizing'] overrides it, e.g. for analysis tools)
GENE_BAND = (.05, .10)                 # the risk gene's band (genome.GENES['risk'])
MARGIN_RANGE = (.02, .10)              # margin as a share of base equity
MAJOR_FACTOR = 2.0
# The stop-risk sizing in force up to the cutover (execution.ACCOUNT_RULES / okx_demo_trader before 2026-11-25), frozen
# like execution.LEGACY_SIZING / PRIOR_SIZING: fills on or before account['margin_sizing_from'] keep it. Never edit.
STOP_RISK_SIZING = dict(min_risk=.05, max_risk=.10, crowd_steps=((10, .5),))


def margin_base(gene, band=GENE_BAND, out=MARGIN_RANGE):
    """The risk gene (missing = band low, clamped into `band`) mapped linearly onto `out`: 5 % → 2 %, 10 % → 10 %."""
    lo, hi = band
    g = lo if gene is None else min(hi, max(lo, float(gene)))
    return out[0] + (g - lo) / (hi - lo) * (out[1] - out[0])


def margin_fraction(gene, major=False, crowd=1.0, factor=1.0, major_factor=MAJOR_FACTOR, out=MARGIN_RANGE):
    """m: margin_base(gene) × crowd × (major_factor for a major), clamped to `out`, THEN × factor (risk_factor; may go
    below out[0], never above 1 ×)."""
    m = margin_base(gene, out=out) * crowd * (major_factor if major else 1.0)
    return min(out[1], max(out[0], m)) * factor


def margin_qty(base, m, lever, px):
    """Coin quantity (before lot rounding): base × m × lever ÷ px; 0 when any input is not positive."""
    if not (base > 0 and m > 0 and lever > 0 and px > 0):
        return 0.0
    return base * m * lever / px


def mark_margin(signals, from_ts):
    """sizing='margin' on every signal (dict of dicts) whose fill time `opened` is after from_ts (the paper's
    margin_sizing_from stamp; None = still pending: nothing marked). Mutates and returns `signals`."""
    if from_ts is None:
        return signals
    for p in (signals or {}).values():
        if int(p.get('opened') or 0) > int(from_ts):
            p['sizing'] = MARGIN_SIZING
    return signals


def position_upl(pos, ct_val):
    """Unrealised PnL (USDT) of one OKX position record: its `upl`, else (markPx − avgPx) × pos × ctVal × side (posSide
    long / short, or the sign of pos in net mode); None when neither can be read."""
    try:
        u = pos.get('upl')
        if u not in (None, ''):
            u = float(u)
            if math.isfinite(u):
                return u
    except (TypeError, ValueError):
        pass
    try:
        n, avg, mark = float(pos.get('pos') or 0), float(pos.get('avgPx') or 0), float(pos.get('markPx') or 0)
        ct = float(ct_val)
        if not (avg > 0 and mark > 0 and ct > 0):
            return None
        ps = pos.get('posSide', 'net')
        side = (1 if n > 0 else -1) if ps == 'net' else (1 if ps == 'long' else -1)
        return abs(n) * ct * (mark - avg) * side
    except (TypeError, ValueError):
        return None


def live_base_equity(equity, positions, own_keys, ct_val):
    """(base, foreign_upl, None) or (None, None, reason) for the OKX follower.
    positions: {(instId, posSide): OKX position} — every USDT swap on the account (Mirror.all_positions);
    own_keys: the keys that are the program's own open, non-adopted mirrors (Mirror.own_position_keys());
    ct_val(inst) → contract value. Every other position (manual, adopted) is not the program's: its unrealised PnL is
    taken out of the exchange equity in both signs (a manual profit does not enlarge our positions, a manual loss does not
    shrink them), and the result is floored at 0. A foreign position whose PnL cannot be read → (None, None, reason): the
    caller skips the margin-sized entry rather than guess."""
    try:
        eq = float(equity)
    except (TypeError, ValueError):
        return None, None, 'equity_unknown'
    if not math.isfinite(eq):
        return None, None, 'equity_unknown'
    foreign = 0.0
    for k, p in (positions or {}).items():
        if k in own_keys:
            continue
        try:
            if not float(p.get('pos') or 0):
                continue
        except (TypeError, ValueError):
            return None, None, f'position_unreadable:{k[0]}'
        try:
            ct = ct_val(k[0])
        except Exception:   # noqa: BLE001 — a spec lookup failure only matters when upl itself is missing
            ct = 0
        u = position_upl(p, ct)
        if u is None:
            return None, None, f'upl_unknown:{k[0]}'
        foreign += u
    return max(0.0, eq - foreign), foreign, None

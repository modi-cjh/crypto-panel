#!/usr/bin/env python3
"""Replay for the trail / risk PR (execution.py TRAIL & RISK RULES): trailing-start / breakeven variants, the same-champion
same-bar risk share, the new / winding seat half risk, current vs new rules and ablations.

Everything runs through the repo's own engines: execution.simulate_account (the paper account) and execution.simulate
(the single-genome fitness backtest); the recent period runs paper.update itself (what run.py does, without evolution).
Offline: reads cached public OKX 1H candles / funding (evolve cache format) and a paper-data snapshot; writes JSON +
markdown into --out. Nothing is written anywhere else (run with python3 -B).

Scenarios
- OOS = 2025-06-03 → 2025-11-30 (the out-of-sample window of the earlier analyses: before the evolution window, no strategy
  has seen it; 250 warm-up bars before the start), on the coins of the universe that traded the whole window.
  * team_static: the current paper team (8 members, the leader first), seated for the whole window. Every seat is
    "old" (tenure_ts far in the past), so the new-seat rule cannot act here; it tests the trail variants and the
    same-bar share.
  * team_churn: the real seat schedule of the paper account (every segment with its genes, start / wind times and
    leader ranges, from the account start to the snapshot) tiled end to end over the window (SYNTHETIC: real genomes
    and real seat turnover, shifted in time). This is where the new / winding seat rule acts.
  * fitness: execution.simulate() of each current team member alone (the evolution's fitness engine); mean of members.
- RECENT = the real paper account replayed by paper.update on the snapshot state with candles up to the newest closed
  bar; the TRAIL & RISK cutover is put at the live start (2026-11-15 14:00 UTC) so the new rules act on that period, and
  the metrics are taken from that point. Short sample (≈ 6 days).
- LIVE3: the three live trades of 2026-11-21 (AVAX, FIL long; ETH short) re-run on 1H candles with the live follow's
  hourly granularity, current vs new rules (R unit, trail / breakeven, and the risk factor the new paper replay gave
  that position).

usage: trail_risk_replay.py --oos-cache DIR --recent-cache DIR --paper DIR --live-trades FILE --out DIR [--only oos,recent,live3]
"""
import argparse
import bisect
import copy
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evolve'))
import execution as ex      # noqa: E402
import paper as P           # noqa: E402
from genome import fix      # noqa: E402

H = ex.HOUR
DAY = 24 * H
WARM = 250
OOS = ('2025-06-03', '2025-11-30')
LIVE_START = int(dt.datetime(2026, 11, 15, 14, tzinfo=dt.timezone.utc).timestamp() * 1000)   # 2026-11-15 21:00 UTC+7
iso = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime('%Y-%m-%d %H:%M')
ms = lambda d: int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

# ------------------------------------------------------------------ variants
CUR_TRAIL = dict(ex.PRIOR_TRAIL)                       # 1R / 1R, no breakeven (PR #110)
OFF = dict(same_bar_share=False, new_seat_h=None)
TRAIL_VARIANTS = {                                     # risk rules off: the trail alone
    'T1.0 (current)': dict(trail=dict(CUR_TRAIL)),
    'T0.75': dict(trail=dict(start_r=.75, dist_r=1.0, be_r=None)),
    'T0.5': dict(trail=dict(start_r=.5, dist_r=1.0, be_r=None)),
    'BE0.5+T1.0': dict(trail=dict(start_r=1.0, dist_r=1.0, be_r=.5)),
    'BE0.5+T0.75': dict(trail=dict(start_r=.75, dist_r=1.0, be_r=.5)),
}
SEAT_VARIANTS = {                                      # trail current: the seat rule alone
    'half none': dict(new_seat_h=None),
    'half 24h': dict(new_seat_h=24),
    'half 48h': dict(new_seat_h=48),
}


def rules_for(v):
    r = dict(ex.ACCOUNT_RULES, trail=dict(CUR_TRAIL), **OFF)
    r.update(v)
    return r


# ------------------------------------------------------------------ data
def load_series(cache, insts, specs, t0, t1):
    """Like data.load_series, offline: closed 1H bars in [t0, t1) on a common timeline; coins that did not trade the
    whole window are left out; funding from cache/funding (OKX's base .0001 every 8 h before the oldest known rate)."""
    cache, rows, fund = Path(cache), {}, {}
    for k in insts:
        p = cache / f'{k}.json'
        if not p.exists():
            continue
        r = [x for x in json.loads(p.read_text())['rows'] if t0 <= x[0] < t1]
        if not r or r[0][0] > t0 + DAY:
            continue
        rows[k] = r
        fp = cache / 'funding' / f'{k}.json'
        fund[k] = json.loads(fp.read_text())['rows'] if fp.exists() else []
    newest = max(r[-1][0] for r in rows.values())
    rows = {k: r for k, r in rows.items() if r[-1][0] >= newest - 3 * H}
    keep = set.intersection(*(set(x[0] for x in r) for r in rows.values()))
    out = {}
    for k, r in rows.items():
        r = [x for x in r if x[0] in keep]
        ts = [x[0] for x in r]
        known = dict(fund[k]); first = fund[k][0][0] if fund[k] else float('inf')
        f = {t: .0001 for t in ts if t < first and t % (8 * H) == 0}
        f.update({t: known[t] for t in ts if t in known})
        out[k] = ex.Series(k, ts, [x[1] for x in r], [x[2] for x in r], [x[3] for x in r], [x[4] for x in r], [x[5] for x in r],
                           spec=tuple(specs[k]) if k in specs else None, funding=f)
    ref = out.get('BTC-USDT-SWAP')
    for k, s in out.items():
        if k != 'BTC-USDT-SWAP':
            s.ref = ref
    return out


# ------------------------------------------------------------------ metrics
def metrics(curve, trades, start_eq, t_from=None):
    """Total return, max drawdown (hourly marks), daily stats on UTC+7 days (day return = last mark of the day vs the
    previous day's last mark), win rate, profit factor, trade count (trades closed in the window)."""
    pts = [(t, e) for t, e in curve if t_from is None or t >= t_from]
    if t_from is not None:
        before = [e for t, e in curve if t < t_from]
        base = before[-1] if before else pts[0][1]
    else:
        base = start_eq
    peak, mdd = base, 0.0
    for _, e in pts:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak > 0 else 1.0)
    days, prev = {}, base
    for t, e in pts:
        days[(t + 7 * H) // DAY] = e
    rets = []
    for d in sorted(days):
        rets.append(days[d] / prev - 1 if prev > 0 else 0.0)
        prev = days[d]
    tr = [x for x in trades if t_from is None or x['closed'] >= t_from]
    w = [x['pnl'] for x in tr if x['pnl'] > 0]
    lo = [x['pnl'] for x in tr if x['pnl'] <= 0]
    end = pts[-1][1]
    ret = (end / base - 1) * 100
    return dict(ret_pct=round(ret, 2), max_dd_pct=round(mdd * 100, 2), ret_over_dd=round(ret / (mdd * 100), 3) if mdd > 0 else None,
                worst_day_pct=round(min(rets) * 100, 2) if rets else None, days_loss_ge10=sum(1 for r in rets if r <= -.10),
                days_loss_ge25=sum(1 for r in rets if r <= -.25), daily_halt_days=len({(x['closed'] + 7 * H) // DAY for x in tr if x['reason'] == 'daily_halt'}),
                mean_daily_pct=round(sum(rets) / len(rets) * 100, 3) if rets else None, days=len(rets),
                win_rate=round(len(w) / len(tr) * 100, 1) if tr else None,
                profit_factor=round(sum(w) / -sum(lo), 3) if lo and sum(lo) else None, trades=len(tr),
                trailing_stops=sum(1 for x in tr if x['reason'] == 'trailing_stop'),
                factored_trades=sum(1 for x in tr if x.get('risk_factor', 1.0) != 1.0), end_equity=round(end, 2))


# ------------------------------------------------------------------ OOS segments
def static_team(state):
    nodes, leader = state['nodes'], state.get('leader')
    team = sorted(state['team'], key=lambda m: m['id'] != leader)
    return [dict(id=m['id'], genes=fix(dict(nodes[m['id']]['genes'])), leader=m['id'] == leader) for m in team]


def tiled_schedule(state, last_ts):
    """The real paper seat schedule [(champion, genes, from offset, wind offset or None, [(boost from, to)])] and its length."""
    acc = state['paper']['account']
    a0 = acc['from_ts']
    length = last_ts - a0 + H
    sched = []
    for g in acc['segs']:
        sched.append(dict(champion=g['champion'], genes=fix(dict(g['genes'])), f=g['from_ts'] - a0,
                          w=(g['wind_ts'] - a0) if g['wind_ts'] is not None else None,
                          boosts=[(a - a0, (b - a0) if b is not None else None) for a, b in g['boosts']]))
    return sched, length


def churn_segs(sched, length, ts, start, end):
    """Tile the schedule over bars [start, end): cycle k shifts every segment by k × length; a seat still open at the end
    of the schedule is wound at the cycle end (the next cycle's first segments then continue it when contiguous)."""
    t0, t_end = ts[start], ts[end - 1]
    idx = lambda t: min(end - 1, max(start, bisect.bisect_left(ts, t)))
    raw, k = [], 0
    while t0 + k * length <= t_end:
        base = t0 + k * length
        for j, g in enumerate(sched):
            f = base + g['f']
            if f > t_end:
                continue
            w = base + (g['w'] if g['w'] is not None else length)
            raw.append(dict(id=f'c{k}_{j}', champion=g['champion'], genes=g['genes'], from_ts=f, wind_ts=w if w <= t_end else None,
                            boosts=[(base + a, base + (b if b is not None else length)) for a, b in g['boosts']]))
        k += 1
    chain, tenure = P.seat_chains(raw)
    return [dict(id=g['id'], genes=g['genes'], from_i=idx(g['from_ts']), wind_i=idx(g['wind_ts']) if g['wind_ts'] is not None else None,
                 boosts=[(idx(a), idx(b) if b <= t_end else None) for a, b in g['boosts'] if a <= t_end],
                 chain=chain[g['id']], tenure_ts=tenure[g['id']]) for g in raw]


def run_oos(args, state, uni, log):
    t0, t1 = ms(OOS[0]) - WARM * H, ms(OOS[1]) + DAY
    series = load_series(args.oos_cache, uni['instruments'], uni.get('specs', {}), t0, t1)
    s0 = next(iter(series.values()))
    n, ts = s0.n, s0.ts
    team = static_team(state)
    sched, length = tiled_schedule(state, max(p[0] for p in json.loads((Path(args.paper) / 'dashboard.json').read_text())['paper']['curve']))
    churn = churn_segs(sched, length, ts, WARM, n)
    info = dict(start=iso(ts[WARM]), end=iso(ts[-1]), coins=len(series), bars=n - WARM, days=round((n - WARM) / 24, 1),
                btc_ret_pct=round((series['BTC-USDT-SWAP'].c[-1] / series['BTC-USDT-SWAP'].c[WARM] - 1) * 100, 2),
                team=[m['id'] for m in team], leader=next((m['id'] for m in team if m['leader']), None),
                churn=dict(schedule_segments=len(sched), cycle_days=round(length / DAY, 2), segments=len(churn),
                           seats=len({g['chain'] for g in churn})))
    log(f"OOS {info['start']} → {info['end']} · {info['coins']} coins · churn {len(churn)} segs / {info['churn']['seats']} seats")
    static = [dict(id=m['id'], genes=m['genes'], from_i=WARM, wind_i=None, boosts=[(WARM, None)] if m['leader'] else [],
                   tenure_ts=-10 ** 15) for m in team]

    def acct(segs, v):
        t = time.time()
        r = ex.simulate_account(copy.deepcopy(segs), series, WARM, n, equity=10000.0, rules=rules_for(v), curve_every=1)
        m = metrics(r['curve'], r['trades'], 10000.0)
        m['liquidations'] = r['liquidations']
        log(f"  {round(time.time() - t, 1)}s {m}")
        return m

    def fit(v):
        rows = []
        for m in team:
            r = ex.simulate(m['genes'], series, WARM, n, equity=10000.0, curve_every=24, rules=rules_for(v))
            rows.append(dict(id=m['id'], fitness=ex.fitness(r['metrics']), **r['metrics']))
        k = len(rows)
        return dict(mean_ret_pct=round(sum(x['ret'] for x in rows) / k, 2), mean_max_dd_pct=round(sum(x['max_dd'] for x in rows) / k, 2),
                    mean_fitness=round(sum(x['fitness'] for x in rows) / k, 4), trades=sum(x['trades'] for x in rows),
                    win_rate=round(sum(x['win_rate'] * x['trades'] for x in rows) / max(1, sum(x['trades'] for x in rows)), 1), members=rows)

    out = dict(info=info, trail={}, seat={}, same_bar={}, compare={})
    for name, v in TRAIL_VARIANTS.items():
        log(f'trail {name}')
        out['trail'][name] = dict(team_static=acct(static, v), team_churn=acct(churn, v), fitness=fit(v))
    for name, v in dict(**{'share off': dict(same_bar_share=False)}, **{'share on': dict(same_bar_share=True)}).items():
        log(f'same-bar {name}')
        out['same_bar'][name] = dict(team_static=acct(static, v), team_churn=acct(churn, v), fitness=fit(v))
    for name, v in SEAT_VARIANTS.items():
        log(f'seat {name}')
        out['seat'][name] = dict(team_churn=acct(churn, v))
    return out, (series, static, churn, acct, fit)


# ------------------------------------------------------------------ recent: the real paper account
def recent_series(args, uni):
    last = json.loads((Path(args.recent_cache) / 'BTC-USDT-SWAP.json').read_text())['rows'][-1][0]
    return load_series(args.recent_cache, uni['instruments'], uni.get('specs', {}), last + H - 365 * DAY, last + H)   # data.py: a 365-day window


def run_recent(args, state, uni, dash, v, series, tag, log):
    """paper.update on the snapshot (changes = [], as run.py without evolution), ACCOUNT_RULES patched to variant v and
    the TRAIL & RISK cutover at the live start. Returns metrics from the live start and the trades / positions."""
    s0 = next(iter(series.values()))
    since = s0.ts[-1] - 365 * DAY
    sr = {k: s for k, s in series.items()}
    st = copy.deepcopy(state)
    st['paper']['account']['trail_risk_from'] = LIVE_START          # cutover at the live start (paper.update keeps it: setdefault)
    saved = dict(ex.ACCOUNT_RULES)
    try:
        ex.ACCOUNT_RULES.update(rules_for(v))
        view, signals = P.update(st, sr, [], uni.get('price_off', {}), prev_last=int(dash['paper']['curve'][-1][0]))
    finally:
        ex.ACCOUNT_RULES.clear(); ex.ACCOUNT_RULES.update(saved)
    acc = st['paper']['account']
    # the full account replay again for curve + every trade (view keeps only the last 400 points / 50 trades)
    res = _replay_account(st, sr, uni, v)
    m = metrics(res['curve'], res['trades'], acc['start_equity'], t_from=LIVE_START)
    by = {g['id']: g['champion'] for g in acc['segs']}
    live_coins = ('AVAX-USDT-SWAP', 'FIL-USDT-SWAP', 'ETH-USDT-SWAP')
    focus = [dict(inst=t['inst'], side=t['side'], champion=by[t['seg']], opened=iso(t['opened']), closed=iso(t['closed']), reason=t['reason'],
                  pnl=round(t['pnl'], 2), risk_factor=t.get('risk_factor', 1.0))
             for t in res['trades'] if t['inst'] in live_coins and t['opened'] >= ms('2026-11-20')]
    focus += [dict(inst=p['inst'], side=p['side'], champion=by[p['seg']], opened=iso(p['opened']), closed=None, reason='open',
                   pnl=None, risk_factor=p.get('risk_factor', 1.0)) for p in res['positions'] if p['inst'] in live_coins and p['opened'] >= ms('2026-11-20')]
    log(f'recent {tag}: {m}')
    return dict(metrics=m, focus=focus, last_bar=iso(s0.ts[-1]), first_bar=iso(s0.ts[0]), equity=view['equity'],
                signals={k: dict(side=x['side'], champion=x.get('champion'), risk_factor=x.get('risk_factor'), seat_from=iso(x['seat_from']) if x.get('seat_from') else None,
                                 winding=x.get('winding')) for k, x in signals.items()})


def _replay_account(st, series, uni, v):
    """The same simulate_account call paper.update makes (after its stamps were set), for the full curve and trades."""
    acc = st['paper']['account']
    s0 = next(iter(series.values()))
    idx = lambda t: P._index(series, t)
    chain, tenure = P.seat_chains(acc['segs'])
    segs = [dict(id=g['id'], genes=fix(dict(g['genes'])), from_i=idx(g['from_ts']), wind_i=idx(g['wind_ts']) if g['wind_ts'] is not None else None,
                 chain=chain[g['id']], tenure_ts=tenure[g['id']],
                 boosts=[(idx(a), idx(b) if b is not None else None) for a, b in g['boosts']],
                 **({'old_risk': g['genes']['risk']} if g['genes'].get('risk') is not None else {}),
                 old_exit={k: g['genes'][k] for k in ('target_rr', 'trail', 'max_hold', 'tf') if g['genes'].get(k) is not None}) for g in acc['segs']]
    price_off = uni.get('price_off', {})
    blocked = {inst: [(idx(a), idx(b) if b is not None else None) for a, b in rs] for inst, rs in price_off.items()}
    mfrom = acc['manual_from']
    manual = {(inst, int(o)): (at, px, idx(mfrom[f'{inst}|{int(o)}'])) for (inst, o), (at, px) in P.MANUAL_CLOSES.items()}
    return ex.simulate_account(segs, series, idx(acc['from_ts']), s0.n, equity=acc['start_equity'], rules=rules_for(v), blocked=blocked,
                               side_cap_from=idx(acc['side_cap_from']), risk_from=idx(acc['risk_rules_from']),
                               risk_cap_from=idx(acc['risk_cap_from']), exit_from=idx(acc['exit_rules_from']), manual=manual,
                               trail_risk_from=idx(acc['trail_risk_from']))


# ------------------------------------------------------------------ the three live trades
def live3(args, series, trail, factors, use_live_unit):
    """Re-run AVAX / FIL / ETH on 1H candles at the live follow's granularity: the stop / target are checked inside each
    bar (stop first), the trail is recomputed at the :50 run after each closed bar and takes effect from the next bar.
    Size × the risk factor of that position in the new paper replay (1 = current)."""
    rows = []
    for t in json.loads(Path(args.live_trades).read_text()):
        if t['inst'] not in ('AVAX', 'FIL', 'ETH') or t['open_ms'] < ms('2026-11-20'):
            continue                                       # the three trades opened 2026-11-20 17:50Z / 2026-11-21 01:50Z
        inst = f"{t['inst']}-USDT-SWAP"
        s = series[inst]
        side = 1 if t['side'] == '多' else -1
        entry, stop0, target = t['entry_fill'], t['init_stop'], t['target']
        paper_dist = {'AVAX': 0.26963327797044995, 'FIL': 0.02736698332048586, 'ETH': 88.31595156230033}[t['inst']]   # live state 'dist'
        unit = ex.trail_unit(paper_dist, abs(entry - stop0)) if use_live_unit else paper_dist
        f = factors.get((t['inst'], 1 if t['side'] == '多' else -1, t['champion']), 1.0)
        units = t['contracts'] * t['ctval'] * f
        stop, best, pending, out = stop0, entry, None, None
        first = (t['open_ms'] // H + 1) * H
        for i in range(s.n):
            if s.ts[i] < first:
                continue
            if pending is not None and (pending - stop) * side > 0:
                stop = pending
            pending = None
            o, h, lo, c = s.o[i], s.h[i], s.l[i], s.c[i]
            if (lo <= stop) if side == 1 else (h >= stop):
                out = ((min(o, stop) if side == 1 else max(o, stop)), s.ts[i], 'stop' if stop == stop0 else 'trailing_stop'); break
            if (h >= target) if side == 1 else (lo <= target):
                out = (target, s.ts[i], 'target'); break
            best = max(best, h) if side == 1 else min(best, lo)
            new, on = ex.trail_stop_level(side, entry, best, stop, unit, trail)
            if on:
                pending = new
        if out is None:
            out = (s.c[-1], s.ts[-1] + H, 'open (mark)')
        px, when, why = out
        price = units * (px - entry) * side
        costs = (t['fee_open'] + t['fee_close'] + t['funding']) * f
        rows.append(dict(inst=t['inst'], side=t['side'], champion=t['champion'], entry=entry, init_stop=stop0, paper_dist=round(paper_dist, 6),
                         live_dist=round(abs(entry - stop0), 6), unit=round(unit, 6), best=best, risk_factor=f, exit=round(px, 6), exit_utc=iso(when),
                         why=why, price_pnl=round(price, 3), net_est=round(price + costs, 3), actual_net=t['net']))
    return rows


# ------------------------------------------------------------------ markdown
COLS = [('ret_pct', '收益%'), ('max_dd_pct', '最大回撤%'), ('ret_over_dd', '收益/回撤'), ('worst_day_pct', '最差日%'),
        ('days_loss_ge10', '日亏≥10%天数'), ('days_loss_ge25', '日亏≥25%天数'), ('daily_halt_days', '日亏停手天数'),
        ('mean_daily_pct', '日均%'), ('win_rate', '胜率%'), ('profit_factor', '利润因子'), ('trades', '笔数'),
        ('trailing_stops', '移动止损笔数'), ('factored_trades', '降风险笔数')]


def table(rows, title):
    head = '| 方案 | ' + ' | '.join(c[1] for c in COLS) + ' |'
    lines = [f'**{title}**', '', head, '|' + '---|' * (len(COLS) + 1)]
    for name, m in rows:
        lines.append(f'| {name} | ' + ' | '.join('–' if m.get(k) is None else str(m.get(k)) for k, _ in COLS) + ' |')
    return '\n'.join(lines) + '\n'


def fit_table(rows, title):
    lines = [f'**{title}**', '', '| 方案 | 成员平均收益% | 成员平均回撤% | 平均适应度 | 笔数 | 胜率% |', '|---|---|---|---|---|---|']
    for name, f in rows:
        lines.append(f"| {name} | {f['mean_ret_pct']} | {f['mean_max_dd_pct']} | {f['mean_fitness']} | {f['trades']} | {f['win_rate']} |")
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--oos-cache'); ap.add_argument('--recent-cache'); ap.add_argument('--paper'); ap.add_argument('--live-trades')
    ap.add_argument('--out'); ap.add_argument('--only', default='oos,recent,live3')
    ap.add_argument('--new', default='', help='JSON of the NEW variant (trail / same_bar_share / new_seat_h); default = execution defaults')
    args = ap.parse_args()
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    logf = open(out_dir / 'replay.log', 'a')

    def log(msg):
        print(msg, flush=True); logf.write(msg + '\n'); logf.flush()
    paper = Path(args.paper)
    state = json.loads((paper / 'state.json').read_text())
    uni = json.loads((paper / 'universe.json').read_text())
    dash = json.loads((paper / 'dashboard.json').read_text())
    NEW = json.loads(args.new) if args.new else dict(trail=dict(ex.TRAIL), same_bar_share=ex.SAME_BAR_SHARE, new_seat_h=ex.NEW_SEAT_HOURS)
    CUR = dict(trail=dict(CUR_TRAIL), **OFF)
    ablate = {'current': CUR, 'trail/BE only': dict(CUR, trail=NEW['trail']), 'same-bar only': dict(CUR, same_bar_share=NEW['same_bar_share']),
              'half-risk only': dict(CUR, new_seat_h=NEW['new_seat_h']), 'NEW (all)': NEW}
    result = dict(new=NEW, generated=iso(int(time.time() * 1000)))
    md = [f"# Trail / risk replay\n\nNEW = `{json.dumps(NEW)}`\n"]
    only = set(args.only.split(','))
    if 'oos' in only:
        oos, (series, static, churn, acct, fit) = run_oos(args, state, uni, log)
        for name, v in ablate.items():
            log(f'compare {name}')
            oos['compare'][name] = dict(team_static=acct(static, v), team_churn=acct(churn, v), fitness=fit(v))
        result['oos'] = oos
        i = oos['info']
        md.append(f"## OOS {i['start']} → {i['end']} UTC（{i['days']} 天，{i['coins']} 币，BTC {i['btc_ret_pct']}%）\n\n"
                  f"team_static = 现任 8 人 {i['team']}（第一名 {i['leader']}）全程在座；team_churn = 纸面真实席位表（{i['churn']['schedule_segments']} 段，"
                  f"{i['churn']['cycle_days']} 天一轮）平铺到窗口上（合成：真实基因与换席频率，时间平移），共 {i['churn']['segments']} 段 / {i['churn']['seats']} 个席位。起始 10,000。\n")
        for key, title in (('trail', '移动止损变体（风险规则关）'), ('same_bar', '同冠军同 K 线同向合并风险（移动止损现行）'),
                           ('seat', '新上任 / 离任席位半风险（移动止损现行；只在 team_churn 起作用）'), ('compare', '现行 vs 新规则 与 单项消融')):
            for scen in ('team_static', 'team_churn'):
                rows = [(n, x[scen]) for n, x in oos[key].items() if scen in x]
                if rows:
                    md.append(table(rows, f'{title} · {scen}'))
            frows = [(n, x['fitness']) for n, x in oos[key].items() if 'fitness' in x]
            if frows:
                md.append(fit_table(frows, f'{title} · 单策略适应度引擎 simulate()（现任 8 人各自回测的平均）'))
    if 'recent' in only or 'live3' in only:
        rs = recent_series(args, uni)
        rec = {}
        if 'recent' in only:
            variants = dict(ablate)
            variants.update({f'trail {k}': v for k, v in TRAIL_VARIANTS.items()})
            variants.update({f'seat {k}': dict(CUR, **v) for k, v in SEAT_VARIANTS.items()})
            for name, v in variants.items():
                rec[name] = run_recent(args, state, uni, dash, v, rs, name, log)
            result['recent'] = rec
            md.append(f"## 近期（纸面账户真实席位，{iso(LIVE_START)} UTC → {rec['current']['last_bar']} UTC，约 {rec['current']['metrics']['days']} 天）\n\n"
                      "paper.update 在 paper-data e7d532b 快照上重放，TRAIL & RISK 生效点放在实盘开始（2026-11-15 14:00 UTC）；指标从该点起算。样本很短。\n")
            md.append(table([(n, x['metrics']) for n, x in rec.items()], '近期 · 纸面账户'))
            md.append('**AVAX / FIL / ETH 在纸面重放中的结果**\n\n| 方案 | 币 | 冠军 | 开仓 UTC | 平仓 UTC | 原因 | 纸面盈亏 | 风险系数 |\n|---|---|---|---|---|---|---|---|')
            for n in ablate:
                for f in rec[n]['focus']:
                    md.append(f"| {n} | {f['inst'].split('-')[0]} {'多' if f['side'] == 1 else '空'} | {f['champion']} | {f['opened']} | {f['closed'] or '–'} | {f['reason']} | {f['pnl'] if f['pnl'] is not None else '–'} | {f['risk_factor']} |")
            md.append('')
        if 'live3' in only:
            new_f = {}
            for f in (rec.get('NEW (all)') or run_recent(args, state, uni, dash, NEW, rs, 'NEW (all)', log))['focus']:
                if f['opened'] >= '2026-11-20':             # the same coin, side and champion as the live trade
                    new_f.setdefault((f['inst'].split('-')[0], f['side'], f['champion']), f['risk_factor'])
            l3 = dict(current=live3(args, rs, CUR_TRAIL, {}, False), new=live3(args, rs, NEW['trail'], new_f, True),
                      trail_only=live3(args, rs, NEW['trail'], {}, True), unit_only=live3(args, rs, CUR_TRAIL, {}, True),
                      risk_only=live3(args, rs, CUR_TRAIL, new_f, False), factors={'|'.join(map(str, k)): v for k, v in new_f.items()})
            result['live3'] = l3
            md.append('## 实盘 3 笔（AVAX / FIL 多、ETH 空，2026-11-21）按新规则重算\n\n1H K 线、实盘每小时 :50 运行一次改止损（下一根生效），同一根先止损后止盈；'
                      '尺寸 × 新规则纸面重放给该仓的风险系数；手续费与资金费按系数缩放（估算）。\n\n| 方案 | 币 | R 单位 | 风险系数 | 出场 | 出场 UTC | 原因 | 价差U | 净额U（估） | 实际净额U |\n|---|---|---|---|---|---|---|---|---|---|')
            for n in ('current', 'unit_only', 'trail_only', 'risk_only', 'new'):
                for r in l3[n]:
                    md.append(f"| {n} | {r['inst']} {r['side']} | {r['unit']} | {r['risk_factor']} | {r['exit']} | {r['exit_utc']} | {r['why']} | {r['price_pnl']} | {r['net_est']} | {r['actual_net']} |")
                md.append(f"| {n} 合计 | | | | | | | {round(sum(r['price_pnl'] for r in l3[n]), 3)} | {round(sum(r['net_est'] for r in l3[n]), 3)} | {round(sum(r['actual_net'] for r in l3[n]), 3)} |")
            md.append('')
    tag = args.only.replace(',', '_')
    (out_dir / f'replay_{tag}.json').write_text(json.dumps(result, indent=1, ensure_ascii=False))
    (out_dir / f'replay_{tag}.md').write_text('\n'.join(md))
    log(f'wrote {out_dir}/replay_{tag}.json / .md')


if __name__ == '__main__':
    main()

"""Paper trading for the champion team: ONE shared account, run by execution.simulate_account().

The team's strategies all trade the same account: every position is sized on the whole account's equity (the strategy's
risk gene, 5–10 %, missing = 5 %, then major-coin doubling and crowding (half from the 10th same-side position on), capped at
10 %). Every seat, including the leader, uses its own risk and leverage genes (leverage 1–20×, clamped to that coin's OKX
max). Margin is shared and cross (全仓): liquidation is when account equity falls to or below total maintenance margin,
and then every position is closed. There is no same-side total-risk cap (ACCOUNT_RULES['same_side_cap'] = None); the
daily halt (25 %) applies to everyone, including the leader, and so does the same-side count (at most
ACCOUNT_RULES['max_same_side'] = 20 open positions on one side; only for entries after account['side_cap_from'], the
first run that had it, so positions already open are not replayed away). Sizing changes work the same way, in eras,
so fills that already happened (including positions still open) keep their original size on every replay:
- fills on or before account['risk_rules_from'] (the first run with PR #107): execution.LEGACY_SIZING (2–10 %, cap
  10 %, half at 3 / quarter at 6) with their seat's stored risk gene;
- fills after it and on or before account['risk_cap_from'] (the first run with the 5–10 % band): execution.PRIOR_SIZING
  (5–20 %, cap 20 %, half at 10) with their seat's stored risk gene;
- later fills: the current ACCOUNT_RULES (5–10 %, cap 10 %, half at 10).
Both stamps are set once (setdefault) and never moved.
Exit rules (2026-11-21, execution.py EXIT RULES: stop + take-profit on every position, reverse-signal close per
ACCOUNT_RULES['reverse_close'], trailing stop for every position, no time exit) work the same way:
account['exit_rules_from'] = the last bar of the previous run (the last bar the published history covers; this run's last
bar when unknown), set once. Bars up to it replay with the old exits (target only when the stored gene had one, trail
gene, max_hold / MAX_HOLD_H time exit), so trades that already closed are never rewritten; later bars use the new exits
for every position, including ones opened earlier (they get a take-profit and the trailing stop at the first new bar).
Manual closes (MANUAL_CLOSES below, exit reason `manual_user`): a position the user closed by hand is closed in every
replay at the given price, matched by coin AND entry time (execution.simulate_account `manual`). Like the other eras,
account['manual_from'][key] = the last bar of the previous run (set once per close, when it first appears) so the bars
already published replay unchanged: the close is booked at the open of the first bar at or after its time AND after that
stamp (`closed` = its real time). From then on the coin takes new entries like any other and the position no longer
counts for the same-side count / crowding.
Trail & risk rules (2026-11-21, execution.py TRAIL & RISK RULES: trailing start / breakeven, same-champion same-bar
risk share, new / winding seat half risk) work the same way: account['trail_risk_from'] = the last bar of the previous
run (set once, never moved). Bars up to it trail with execution.PRIOR_TRAIL and fills up to it get no risk factor; later
bars trail every open position under execution.TRAIL and later fills get execution.entry_risk_factor(). The seat's
tenure (`tenure_ts`) is the start of the champion's continuous seat: a segment that starts exactly when the previous
segment of the same champion was wound continues its seat (`chain`). signals.json carries each position's
`risk_factor` (and `seat_from`, `winding`) so the OKX follow sizes the same way.
The OKX follow (tools/okx_demo_trader.py, demo and live) uses the
same 5–10 % / 10 % / half-at-10 steps and 1–20× genes on cross margin, clamped to each coin's OKX max leverage (no new
entry if the spec is missing).
Each run replays the account from its start bar with the full seat history, so the result is exactly what the engine
produces on live bars:
  account = dict(from_ts, start_equity, segs=[dict(id, champion, slot, genes, species, from_ts, wind_ts, boosts, reason)])
- a member that loses its seat gets wind_ts: no new signals, its open positions run to their own exits;
- `boosts` are the [from_ts, to_ts] ranges in which the member was the team leader (state['leader']: the best member that
  passed the full entry gate — out-of-sample and final gate, defense streak ≥ 5, combined-OOS drawdown ≤ 30 %; nobody
  when no member qualifies). The title only decides signal priority, not a bigger size.
Before this, the account was five separate slots of 1/5 each; that history (curve, trades, seat changes) is kept as
paper['legacy'] and the shared account starts from the slots' total equity. The slot code below `_legacy_update` only
serves that one-time conversion.

NEXT-HOUR RULE (2026-11-22, for the hourly server signals). The GitHub paper run (every 4 h, usually ~1 h late) stamps
every change it makes on the EFFECTIVE BAR C = floor(now, 1 h) + 1 h (effective_bar(); `now` = the run's wall clock in
ms UTC, read just before price_check / update; C is a bar OPEN time). C is always later than the run's last bar, so
the change is PENDING when stamped (recorded in state.json only as its timestamp; no separate flag that could go stale)
and takes effect on whichever later replay first has bar C:
- seat in / out (seg from_ts / wind_ts = C) and leader title (boosts [C, …] / closing it at C): the new seat's first
  signal, and the leader's priority, is on the CLOSE of bar C; the old seat's last signal is on the close of bar C − 1 h;
- price_off ranges (data.price_check, stamps are FILL bars): [C + 1 h, …] — fills from the open of bar C + 1 h on
  (paper only: the entries they refuse are published for the OKX live follower as price_off_live, _price_off_live);
- manual closes (account['manual_from'][key] = C): booked at the open of the first bar after C at or after the
  close time, i.e. no earlier than the open of bar C + 1 h;
- cutover keys set for the first time (setdefault; side_cap_from, risk_rules_from, risk_cap_from, exit_rules_from,
  trail_risk_from and every future key, via cutover()): C — bar C replays under the old rule, bar C + 1 h on under
  the new one.
So nothing the replay derives from bars with open time <= C − 1 h changes, including the entries it queues for the
open of bar C: a server that replays at C:01:30 (last bar C − 1 h) gets the same signals from the old or the new
state.json, and from C + 1 h:01:30 on (last bar >= C) it gets what the next GitHub run will replay — as long as the new
state.json is readable by then (the run commits within minutes of `now`; raw CDN cache <= ~5 min).
account['next_hour_from'] = C of the first run with this rule (set once): a record that the state follows it (a
replay-only caller, replay=True, refuses a state without it). Stamps >= it map to the first bar at or after them
(stamps are always on the hour, so this only matters if the timeline ever skipped that hour); older stamps keep the
old mapping (last bar at or before), so published history replays byte-identically. A stamp after the last bar maps
to a virtual index >= n (n = the next, not yet existing bar), i.e. not in effect. Without `now` (tests, analysis
tools) update() keeps the old stamps (this run's last bar / the previous run's last bar).
"""
import copy
from bisect import bisect_left
from execution import simulate, simulate_account, stop_distance, START_EQUITY, ACCOUNT_RULES, MAX_POSITIONS, DEFAULT_LEVERAGE, same_side_full, clamp_risk, target_rr
from execution import entry_risk_factor, live_same_bar_groups, same_bar_groups
from evolution import TEAM_SIZE, entry_gate
from genome import fix

HOUR = 3600_000
EXIT_WINDOW = 24 * HOUR      # closes this recent are published with their reason, so the OKX follow can log why it closes
# Caps (not a fixed leader size): published on the dashboard and used as leverage/risk ceilings.
LEADER_RISK, LEADER_LEVERAGE = ACCOUNT_RULES['leader_risk'], ACCOUNT_RULES['leader_lever']
# The user's manual closes, kept for every replay: (inst, opened ms) → (close time ms, close price). Only the position of
# that coin opened at exactly that time is closed (reason `manual_user`, at that price as filled: taker fee, no slip), at
# the open of the first 1H bar at or after the close time that the published history does not cover yet
# (account['manual_from']); see execution.simulate_account. Add new entries; never edit or remove old ones.
MANUAL_CLOSES = {
    # 2026-11-20T22:19:27Z — closed by hand on OKX live (tools/ops_manual_close.py, reason manual_user) at these fills.
    # The paper history up to 2026-11-21T07:00Z was already published with them open, so they are booked at the first
    # bar after the previous run's last bar (account['manual_from']), at these prices; `closed` keeps 22:19:27Z.
    ('NEAR-USDT-SWAP', 1794700800000): (1795213167000, 2.87),
    ('ADA-USDT-SWAP', 1794700800000): (1795213167000, 0.1436),
    ('LTC-USDT-SWAP', 1794700800000): (1795213167000, 40.59),
    ('KAITO-USDT-SWAP', 1794816000000): (1795213167000, 0.1572),
    ('ICP-USDT-SWAP', 1794931200000): (1795213167000, 2.048),
    # never followed on live: closed at the close of the last completed OKX 1H candle when the user asked (public
    # market/candles?instId=…&bar=1H&limit=3, fetched 2026-11-21T12:24:55Z): bar 2026-11-21T11:00Z (open 1795258800000,
    # confirm 1), close time 12:00Z (1795262400000) → booked at the open of the 12:00Z bar (or the first unpublished bar).
    ('H-USDT-SWAP', 1794384000000): (1795262400000, 0.01098),
    ('DOGE-USDT-SWAP', 1794499200000): (1795262400000, 0.05372),
}
# Account keys that switch a rule on from a bar (eras); set once with cutover() and never moved. Add new keys here.
CUTOVER_KEYS = ('side_cap_from', 'risk_rules_from', 'risk_cap_from', 'exit_rules_from', 'trail_risk_from', 'price_off_live_from')
_CTX = dict(extra={})        # per update(): {slot index: {ts: equity of the other slots}} for boosted segments


def _size(genes, leader=False):
    """Per-trade risk and requested leverage from the genome (clamped). The leader flag is for priority only."""
    g = fix(dict(genes))
    return dict(risk=clamp_risk(g.get('risk'), ACCOUNT_RULES['min_risk'], ACCOUNT_RULES['max_risk']),
                leverage=int(g.get('leverage', DEFAULT_LEVERAGE)),
                **({'leader': True} if leader else {}))


def _leader_view(leader, nodes=None):
    """Dashboard card for the current leader: its gene values, plus the account caps."""
    if not leader:
        return None
    genes = (nodes or {}).get(leader, {}).get('genes') or {}
    g = fix(dict(genes)) if genes else None
    return dict(id=leader,
                risk=(clamp_risk(g.get('risk'), ACCOUNT_RULES['min_risk'], ACCOUNT_RULES['max_risk']) if g else ACCOUNT_RULES['max_risk']),
                leverage=int(g.get('leverage', DEFAULT_LEVERAGE) if g else ACCOUNT_RULES['leader_lever']),
                max_risk=ACCOUNT_RULES['max_risk'], max_leverage=ACCOUNT_RULES['leader_lever'])


def seat_chains(segs):
    """({seg id: seat chain}, {seg id: seat start ts}) for execution TRAIL & RISK RULES c: a segment that starts exactly
    when the previous segment of the same champion was wound (a slot move, a re-seat in the same run) continues that
    seat; any other segment starts a new seat at its from_ts. segs in account order (dicts with id, champion, from_ts,
    wind_ts)."""
    chain, tenure, last = {}, {}, {}
    for g in segs:
        prev = last.get(g['champion'])
        if prev is not None and prev['wind_ts'] is not None and prev['wind_ts'] == g['from_ts']:
            chain[g['id']], tenure[g['id']] = chain[prev['id']], tenure[prev['id']]
        else:
            chain[g['id']], tenure[g['id']] = g['id'], g['from_ts']
        last[g['champion']] = g
    return chain, tenure


class StateDrift(ValueError):
    """update(replay=True): the state would need a change (seat / leader / cutover) that only the GitHub run may stamp."""


def effective_bar(now_ms):
    """The effective bar C of a change made by a GitHub paper run at wall clock now_ms (ms UTC): the OPEN time of the bar
    after the current hour, floor(now, 1 h) + 1 h. now = 16:57:12 → 17:00; 17:00:00.000 → 18:00; 16:59:59.999 → 17:00.
    See NEXT-HOUR RULE in the module docstring for what "effective" means for each kind of change."""
    return (int(now_ms) // HOUR + 1) * HOUR


def _index(series, ts):
    s = next(iter(series.values()))
    for i in range(s.n - 1, -1, -1):
        if s.ts[i] <= ts:
            return i
    return 0


def run_segment(seg, genes, series, close_at_end=False, wind_ts=None):
    s = next(iter(series.values()))
    start = _index(series, seg['from_ts'])
    if start >= s.n:
        return dict(equity=seg['start_equity'], trades=[], curve=[(seg['from_ts'], seg['start_equity'])], positions=[], pending={}, metrics=None)
    extra = dict(extra_equity=_CTX['extra'].get(seg.get('slot'), {})) if seg.get('boost') else {}
    return simulate(fix(dict(genes)), series, start, s.n, equity=seg['start_equity'], close_at_end=close_at_end, curve_every=1,
                    stop_entries_at=_index(series, wind_ts) if wind_ts is not None else None, **extra)


def _others_equity(slots, series):
    """{slot index: {ts: summed equity of the OTHER slots}} on the series timeline, from each slot's finished curve and
    its running normal (not boosted) segment. Boosted segments are left out so the leader's size never depends on itself."""
    ts = next(iter(series.values())).ts
    per = []
    for slot in slots:
        pts = [list(p) for p in slot['curve_done']]
        seg = slot['segments'][-1] if slot['segments'] and slot['segments'][-1]['to_ts'] is None else None
        if seg and not seg.get('boost'):
            pts += [[t, e] for t, e in run_segment(seg, seg['genes'], series)['curve']]
        out, k, last = [], 0, slot['start_equity']
        for t in ts:
            while k < len(pts) and pts[k][0] <= t:
                last = pts[k][1]; k += 1
            out.append(last)
        per.append(out)
    total = [sum(col) for col in zip(*per)] if per else []
    return {i: {t: total[j] - per[i][j] for j, t in enumerate(ts)} for i in range(len(slots))}


def _winding(slot, series):
    """Advance the slot's winding-down members. Finished ones (no position left) are booked into slot['carry'].
    Returns (offset to add to the slot's equity, live [(record, result)])."""
    slot.setdefault('winding', []); slot.setdefault('carry', 0.0)
    live = []
    for w in slot['winding']:
        res = run_segment(w, w['genes'], series, wind_ts=w['wind_ts'])
        if res['positions']:
            live.append((w, res))
        else:
            slot['carry'] += res['equity'] - w['base']
            slot['closed_trades'] = (slot['closed_trades'] + [dict(t, champion=w['champion']) for t in res['trades'] if t['closed'] > w['wind_ts']])[-300:]
    slot['winding'] = [w for w, _ in live]
    return slot['carry'] + sum(res['equity'] - w['base'] for w, res in live), live


def _retire(slot, series, last_ts):
    """The running segment loses its seat: no new signals, open positions wind down. Returns the slot's equity now."""
    seg = slot['segments'][-1]
    res = run_segment(seg, seg['genes'], series)
    offset, live = _winding(slot, series)
    seg.update(to_ts=last_ts, end_equity=round(res['equity'], 2), trades=len(res['trades']))
    slot['closed_trades'] = (slot['closed_trades'] + [dict(t, champion=seg['champion']) for t in res['trades']])[-300:]
    slot['curve_done'] = (slot['curve_done'] + [[t, round(e + offset, 2)] for t, e in res['curve']])[-1500:]
    for w, r in live:                       # what the earlier winders made so far is now part of the new start equity
        w['base'] = r['equity']
    slot['carry'] = 0.0
    if res['positions']:
        slot['winding'].append(dict(champion=seg['champion'], species=seg['species'], genes=seg['genes'], from_ts=seg['from_ts'], slot=seg.get('slot'),
                                    start_equity=seg['start_equity'], wind_ts=last_ts, base=res['equity'], boost=bool(seg.get('boost'))))
    return res['equity'] + offset


def _new_slot(equity):
    return dict(start_equity=round(equity, 2), segments=[], closed_trades=[], curve_done=[])


LEGACY_SLOTS = 5          # the old account had five slots of 1/5 each


def _migrate(paper, series):
    """Single-champion paper account → team slots. The old account's equity is split equally across the slots."""
    if 'slots' in paper:
        return paper
    equity = paper.get('start_equity', START_EQUITY)
    if paper.get('segments'):
        seg = paper['segments'][-1]
        equity = run_segment(seg, seg['genes'], series, close_at_end=True)['equity']
    legacy = [{k: v for k, v in sg.items() if k != 'genes'} for sg in paper.get('segments', [])]
    return dict(start_equity=paper.get('start_equity', START_EQUITY), slots=[_new_slot(equity / LEGACY_SLOTS) for _ in range(LEGACY_SLOTS)],
                legacy_segments=legacy, legacy_trades=paper.get('closed_trades', []))


def _close(slot, series, last_ts):
    seg = slot['segments'][-1]
    res = run_segment(seg, seg['genes'], series, close_at_end=True)
    seg.update(to_ts=last_ts, end_equity=round(res['equity'], 2), trades=len(res['trades']))
    slot['closed_trades'] = (slot['closed_trades'] + [dict(t, champion=seg['champion']) for t in res['trades']])[-300:]
    slot['curve_done'] = (slot['curve_done'] + [list(p) for p in res['curve']])[-1500:]
    return res['equity']


def _idle_equity(slot):
    """Equity of a slot with no running segment: where its last member left it, plus finished wind-downs since."""
    base = slot['segments'][-1]['end_equity'] if slot['segments'] else slot['start_equity']
    carry = slot.get('carry', 0.0); slot['carry'] = 0.0
    return base + carry


def _signal_from_pending(inst, d, genes, series, last_ts, member):
    sr = series[inst]
    dist, px, rr = stop_distance(genes, sr, sr.n - 1), sr.c[-1], target_rr(genes)
    return dict(side=d, entry=px, stop=px - d * dist, tp=px + d * rr * dist, dist=dist, opened=last_ts + HOUR,
                strategy='evolved', leverage=int(fix(dict(genes)).get('leverage', DEFAULT_LEVERAGE)), champion=member, pending=True)


def _sum_curves(curves, starts):
    """Account equity per hour: each slot carries its last value forward (and its start value before its first point)."""
    stamps = sorted({p[0] for c in curves for p in c})
    out, idx, last = [], [0] * len(curves), list(starts)
    for ts in stamps:
        for k, c in enumerate(curves):
            while idx[k] < len(c) and c[idx[k]][0] <= ts:
                last[k] = c[idx[k]][1]; idx[k] += 1
        out.append([ts, round(sum(last), 2)])
    return out


def _legacy_update(state, series, changes):
    """The five-slot account (before the shared account): only used once, to read its final state for the conversion."""
    nodes, s = state['nodes'], next(iter(series.values()))
    last_ts = s.ts[-1]
    paper = state['paper'] = _migrate(state.get('paper') or dict(start_equity=START_EQUITY), series)
    slots, team = paper['slots'], state.get('team', [])
    while len(slots) < LEGACY_SLOTS:
        slots.append(_new_slot(paper['start_equity'] / LEGACY_SLOTS))
    leader = state.get('champion')
    _CTX['extra'] = _others_equity(slots, series)
    for slot_i, old, new, reason in changes:
        slot = slots[slot_i]
        equity = _retire(slot, series, last_ts) if slot['segments'] and slot['segments'][-1]['to_ts'] is None else _idle_equity(slot)
        node = nodes[new]
        slot['segments'].append(dict(champion=new, slot=slot_i, genes=fix(node['genes']), species=node['species'], from_ts=last_ts, reason=reason,
                                     start_equity=round(equity, 2), to_ts=None, end_equity=None, boost=new == leader))
    for slot_i, m in enumerate(team):        # a seated member whose slot has no running segment (e.g. after migration) starts one
        slot = slots[slot_i]
        if not (slot['segments'] and slot['segments'][-1]['to_ts'] is None and slot['segments'][-1]['champion'] == m['id']):
            equity = _retire(slot, series, last_ts) if slot['segments'] and slot['segments'][-1]['to_ts'] is None else _idle_equity(slot)
            node = nodes[m['id']]
            slot['segments'].append(dict(champion=m['id'], slot=slot_i, genes=fix(node['genes']), species=node['species'], from_ts=last_ts,
                                         reason='carry', start_equity=round(equity, 2), to_ts=None, end_equity=None, boost=m['id'] == leader))
    for slot_i, m in enumerate(team):        # the leader title moved: the member continues in a new segment with the other size
        slot, seg = slots[slot_i], slots[slot_i]['segments'][-1]
        if bool(seg.get('boost')) != (m['id'] == leader):
            equity = _retire(slot, series, last_ts)
            slot['segments'].append(dict(champion=m['id'], slot=slot_i, genes=seg['genes'], species=seg['species'], from_ts=last_ts,
                                         reason='leader' if m['id'] == leader else 'demoted', start_equity=round(equity, 2), to_ts=None, end_equity=None,
                                         boost=m['id'] == leader))
    _CTX['extra'] = _others_equity(slots, series)
    rank = {m['id']: r for r, m in enumerate(sorted(team, key=lambda m: (m['id'] != leader, m['id'] != state.get('champion'), -nodes[m['id']]['fitness'])))}   # the leader is rank 1
    members, curves, trades, positions, pending, signals = [], [], [], [], [], {}
    order = []
    idle = 0.0
    for slot_i, slot in enumerate(slots):
        offset, live = _winding(slot, series)
        for w, r in live:                       # positions of members that lost the seat, still running to their exits
            positions += [dict(p, champion=w['champion'], slot=slot_i, winding=True) for p in r['positions']]
            trades += [dict(t, champion=w['champion']) for t in r['trades'] if t['closed'] > w['wind_ts']]
            order.append((99, dict(champion=w['champion'], genes=w['genes'], boost=w.get('boost')), dict(r, pending={})))
        seg = slot['segments'][-1] if slot['segments'] and slot['segments'][-1]['to_ts'] is None else None
        if not seg:
            base = slot['segments'][-1]['end_equity'] if slot['segments'] else slot['start_equity']
            idle += base + offset
            curves.append((slot['curve_done'] or [[last_ts, slot['start_equity']]]) + ([[last_ts, round(base + offset, 2)]] if offset else []))
            trades += slot['closed_trades']
            continue
        res = run_segment(seg, seg['genes'], series)
        member = seg['champion']
        curves.append(slot['curve_done'] + [[t, round(e + offset, 2)] for t, e in res['curve']])
        trades += slot['closed_trades'] + [dict(t, champion=member) for t in res['trades']]
        positions += [dict(p, champion=member, slot=slot_i, **({'boost': True} if seg.get('boost') else {})) for p in res['positions']]
        pending += [dict(inst=k, side=v, champion=member) for k, v in (res.get('pending') or {}).items()]
        since = next((m['since'] for m in team if m['id'] == member), None)
        equity = res['equity'] + offset
        members.append(dict(slot=slot_i, id=member, species=seg['species'], since=since, rank=rank.get(member, 99) + 1,
                            start_equity=slot['start_equity'], equity=round(equity, 2),
                            ret=round((equity / slot['start_equity'] - 1) * 100, 3),
                            positions=len(res['positions']), trades=len(slot['closed_trades']) + len(res['trades']), boost=bool(seg.get('boost'))))
        order.append((rank.get(member, 99), seg, res))
    # Mirror signals: one per coin; when several members hold the same coin, the higher-ranked member wins.
    # 'rank' (1 = leader) also orders the OKX mirror's entries when its same-side risk cap fills up.
    for r, seg, res in sorted(order, key=lambda x: x[0]):
        member = seg['champion']
        size = _size(seg['genes'], leader=bool(seg.get('boost')))
        for p in res['positions']:
            signals.setdefault(p['inst'], dict(side=p['side'], entry=p['entry'], stop=p['stop'], tp=p['target'], opened=p['opened'],
                                               strategy='evolved', champion=member, rank=r + 1, **size))
        for inst, d in (res.get('pending') or {}).items():
            signals.setdefault(inst, dict(_signal_from_pending(inst, d, seg['genes'], series, last_ts, member), rank=r + 1, **size))
    curve = _sum_curves(curves, [sl['start_equity'] for sl in slots])
    equity = round(sum(m['equity'] for m in members) + idle, 2)
    segments = sorted(({**{k: v for k, v in sg.items() if k != 'genes'}} for sl in slots for sg in sl['segments']), key=lambda x: x['from_ts'], reverse=True)
    trades = sorted(trades + paper.get('legacy_trades', []), key=lambda t: t['closed'], reverse=True)
    view = dict(equity=equity, start_equity=paper['start_equity'], ret=round((equity / paper['start_equity'] - 1) * 100, 3),
                curve=curve[-400:], trades=trades[:50], positions=positions, pending=pending,
                leader=_leader_view(leader, nodes) if leader else None,
                members=sorted(members, key=lambda m: m['rank']), segments=(segments + paper.get('legacy_segments', [])[::-1])[:30])
    return view, signals


def _convert(state, series, last_ts):
    """Five slots → one shared account, starting now with the slots' total equity (open slot positions count at market)."""
    old = state.get('paper') or {}
    start, legacy = old.get('start_equity', START_EQUITY), dict(curve=[], trades=[], segments=[])
    equity = start
    if old.get('slots') or old.get('segments'):
        view, _ = _legacy_update(state, series, [])
        equity, legacy = view['equity'], dict(curve=view['curve'], trades=view['trades'], segments=view['segments'])
    state['paper'] = dict(start_equity=start, legacy=legacy, account=dict(from_ts=last_ts, start_equity=round(equity, 2), segs=[], next=0))
    return state['paper']


def scheduled(acc, price_off, manual_stamps, last_ts):
    """The changes still pending at last_ts (stamped on a later bar), oldest first: what a replay that has those bars
    will apply. Derived from the stamps only, so it never goes stale."""
    out = []
    for g in acc['segs']:
        if g['from_ts'] > last_ts:
            out.append(dict(kind='seat', ts=g['from_ts'], champion=g['champion'], slot=g['slot'], reason=g['reason']))
        if g['wind_ts'] is not None and g['wind_ts'] > last_ts and g['wind_ts'] != g['from_ts']:
            out.append(dict(kind='wind', ts=g['wind_ts'], champion=g['champion'], slot=g['slot']))
        for a, b in g['boosts']:
            if a > last_ts and a != b:
                out.append(dict(kind='leader', ts=a, champion=g['champion'], slot=g['slot']))
            if b is not None and b > last_ts and a != b:
                out.append(dict(kind='leader_end', ts=b, champion=g['champion'], slot=g['slot']))
    for inst, rs in sorted((price_off or {}).items()):
        for a, b in rs:
            if a > last_ts + HOUR:
                out.append(dict(kind='price_off', ts=a, inst=inst))
            if b is not None and b > last_ts + HOUR and a != b:
                out.append(dict(kind='price_off_end', ts=b, inst=inst))
    for key in CUTOVER_KEYS + ('next_hour_from',):
        if acc.get(key) is not None and acc[key] > last_ts:
            out.append(dict(kind='cutover', ts=acc[key], key=key))
    for key, ts in sorted((manual_stamps or {}).items()):
        if ts > last_ts:
            out.append(dict(kind='manual_close', ts=ts, key=key))
    return sorted(out, key=lambda x: (x['ts'], x['kind']))


def update(state, series, changes, price_off=None, prev_last=None, now=None, replay=False):
    """price_off: data.price_check ranges {inst: [[from_ts, to_ts or None]]} — coins that take no new entry in those ranges.
    prev_last: the last bar of the previous run (its published curve), used once to stamp account['exit_rules_from'] (only
    without `now`).
    now: the GitHub run's wall clock (ms): every change is stamped on effective_bar(now) (NEXT-HOUR RULE). None = the old
    stamps (this run's last bar), for tests and analysis tools.
    replay=True: replay only (the hourly server signals): nothing is stamped and the state is never changed; raises
    StateDrift when the state predates the next-hour rule or its seats / leader disagree with state['team'] /
    state['leader'] (then only a GitHub run may fix it). A cutover key or manual close the state does not have yet is
    pending (old rule on every bar / not booked) — exactly what the published state means until GitHub stamps it.
    Apply this run's seat and leader changes, replay the shared account, and return (dashboard view, mirror signals)."""
    nodes, s = state['nodes'], next(iter(series.values()))
    last_ts = s.ts[-1]
    paper = state.get('paper') or {}
    if 'account' not in paper:
        if replay:
            raise StateDrift('no shared paper account in state')
        paper = _convert(state, series, last_ts)
    acc, team = paper['account'], state.get('team', [])
    if replay and acc.get('next_hour_from') is None:
        raise StateDrift('state predates the next-hour rule (no account.next_hour_from)')
    # the bar every change of this run is stamped on (NEXT-HOUR RULE): never this run's last bar or earlier
    eff = max(effective_bar(now), last_ts + HOUR) if now is not None else last_ts
    if now is not None and not replay:
        acc.setdefault('next_hour_from', eff)
    # old-style stamp of the cutover keys (without `now`): right after the bars the previous run already published
    published = min(prev_last, last_ts) if prev_last is not None else last_ts

    def cutover(key, legacy):
        """An era key: set once (never moved), on the effective bar; replay: a missing key is pending (None here)."""
        if replay:
            return acc.get(key)
        return acc.setdefault(key, eff if now is not None else legacy)
    # Only members who pass the three-part entry gate may keep or take a live paper seat / OKX follow.
    # Failing members are wound down (open positions run to their own exits). If nobody passes, the
    # account holds cash — it never falls back to an ungated strategy. Existing open size is not resized.
    allowed = lambda nid: bool(nid) and nid in nodes and entry_gate(nodes[nid], state=state)['passed']
    leader = state.get('leader') if allowed(state.get('leader')) else None

    def reconcile(acc):
        """Apply this run's seat / leader changes to acc, every stamp on `eff` (NEXT-HOUR RULE)."""
        seated = lambda slot_i: next((g for g in acc['segs'] if g['slot'] == slot_i and g['wind_ts'] is None), None)

        def wind(g):
            g['wind_ts'] = max(eff, g['from_ts'])
            if g['boosts'] and g['boosts'][-1][1] is None:
                g['boosts'][-1][1] = max(eff, g['boosts'][-1][0])

        def seat(slot_i, nid, reason):
            cur = seated(slot_i)
            if cur:
                wind(cur)
            acc['next'] += 1
            acc['segs'].append(dict(id=f"a{acc['next']}", champion=nid, slot=slot_i, genes=fix(nodes[nid]['genes']), species=nodes[nid]['species'],
                                    from_ts=eff, wind_ts=None, boosts=[], reason=reason))

        for slot_i, old, new, reason in changes:
            if allowed(new):
                seat(slot_i, new, reason)
            elif seated(slot_i):
                wind(seated(slot_i))
        for slot_i, m in enumerate(team):                    # a seated member without a running segment (first run, conversion) starts one
            cur = seated(slot_i)
            if not allowed(m['id']):
                if cur:
                    wind(cur)
                continue
            if not cur or cur['champion'] != m['id']:
                seat(slot_i, m['id'], 'carry')
        for g in acc['segs']:                                # a seat that no longer exists, or no longer passes the entry gate
            if g['wind_ts'] is None and (g['slot'] >= len(team) or team[g['slot']]['id'] != g['champion'] or not allowed(g['champion'])):
                wind(g)
        for g in acc['segs']:                                # the leader title — only an entry-gated member may hold the bigger size
            if g['wind_ts'] is None:
                is_leader = bool(g['boosts']) and g['boosts'][-1][1] is None
                if g['champion'] == leader and not is_leader:
                    g['boosts'].append([max(eff, g['from_ts']), None])
                elif g['champion'] != leader and is_leader:
                    g['boosts'][-1][1] = max(eff, g['boosts'][-1][0])

    if replay:                                           # the server never stamps: the published state must already agree
        probe = copy.deepcopy(acc)
        reconcile(probe)
        if probe != acc:
            raise StateDrift('seats / leader in state.json disagree with its team / leader')
    else:
        reconcile(acc)
    n, nh = s.n, acc.get('next_hour_from')

    def idx(ts):
        """Bar index of a stamp. After the last bar: a virtual index >= n (n = the next bar), i.e. not in effect yet.
        Stamps >= next_hour_from: the first bar at or after them; older stamps: the last bar at or before (as always)."""
        if ts > last_ts:
            return n - 1 + -(-(ts - last_ts) // HOUR)
        if nh is not None and ts >= nh:
            return bisect_left(s.ts, ts)
        return _index(series, ts)
    # genes are re-clamped to the current bands for new entries; old_risk = the risk gene as stored when the seat was
    # taken (fix() under the band of that time), used for fills on or before risk_rules_from / risk_cap_from so the
    # replay keeps their size (each era clamps it into its own band).
    chain, tenure = seat_chains(acc['segs'])
    segs = [dict(id=g['id'], genes=fix(dict(g['genes'])), from_i=idx(g['from_ts']), wind_i=idx(g['wind_ts']) if g['wind_ts'] is not None else None,
                 chain=chain[g['id']], tenure_ts=tenure[g['id']],
                 boosts=[(idx(a), idx(b) if b is not None else None) for a, b in g['boosts']],
                 **({'old_risk': g['genes']['risk']} if g['genes'].get('risk') is not None else {}),
                 # the exit genes as stored (old fix(): target_rr 0 = no target), for bars on or before exit_rules_from
                 old_exit={k: g['genes'][k] for k in ('target_rr', 'trail', 'max_hold', 'tf') if g['genes'].get(k) is not None}) for g in acc['segs']]
    blocked = {inst: [(idx(a), idx(b) if b is not None else None) for a, b in rs] for inst, rs in (price_off or {}).items()}
    off_now = {inst for inst, rs in (price_off or {}).items() if rs and rs[-1][1] is None}      # shown on the dashboard
    # coins blocked for the next fill (the open of bar n): the same test the next replay applies at that bar
    blocked_next = {inst for inst, rs in blocked.items() if any(a <= n and (b is None or n < b) for a, b in rs)}
    # the era keys (CUTOVER_KEYS, each set once). Without `now` the stamps of their time: this run's last bar, or for the
    # exits / trail & risk rules the previous run's last bar (right after the bars already published; closed trades stay)
    cut = {k: cutover(k, published if k in ('exit_rules_from', 'trail_risk_from') else last_ts) for k in CUTOVER_KEYS}
    # a key that is still pending (replay of a state GitHub has not stamped yet) = the old rule on every bar so far
    ci = {k: idx(v) if v is not None else n for k, v in cut.items()}
    # each manual close starts after the effective bar (old: after the bars already published); set once per close
    mfrom = acc.get('manual_from', {}) if replay else acc.setdefault('manual_from', {})
    if not replay:
        for inst, opened in MANUAL_CLOSES:
            mfrom.setdefault(f'{inst}|{int(opened)}', eff if now is not None else published)
    manual = {(inst, int(opened)): (at, px, idx(mfrom[f'{inst}|{int(opened)}'])) for (inst, opened), (at, px) in MANUAL_CLOSES.items()
              if f'{inst}|{int(opened)}' in mfrom}       # replay: a close GitHub has not stamped yet is pending
    res = simulate_account(segs, series, idx(acc['from_ts']), s.n, equity=acc['start_equity'], blocked=blocked,
                           side_cap_from=ci['side_cap_from'], risk_from=ci['risk_rules_from'],
                           risk_cap_from=ci['risk_cap_from'], exit_from=ci['exit_rules_from'], manual=manual,
                           trail_risk_from=ci['trail_risk_from'], off_book_from=ci['price_off_live_from'])
    by = {g['id']: g for g in acc['segs']}
    rank = {m['id']: r for r, m in enumerate(sorted(team, key=lambda m: (m['id'] != leader, -nodes[m['id']]['fitness'])))}   # the leader is rank 1
    who = lambda sid: by[sid]['champion']
    tag = lambda x: dict({k: v for k, v in x.items() if k != 'seg'}, champion=who(x['seg']), slot=by[x['seg']]['slot'])
    # a seat whose wind stamp is still pending (> last bar) keeps trading until that bar: not winding yet
    wound = lambda g: g['wind_ts'] is not None and g['wind_ts'] <= last_ts
    positions = [dict(tag(p), **({'winding': True} if wound(by[p['seg']]) else {})) for p in res['positions']]
    trades = [tag(t) for t in res['trades']]
    # Signals waiting for the next open, as far as the account will take them: one per coin, the leader's first, and no more
    # than a strategy's free position slots (otherwise the mirror would open entries the account itself never fills).
    # A manual close booked at the open of that next bar (before its fills) no longer counts, as in the next replay.
    nxt = last_ts + HOUR
    books_next = {(inst, o) for (inst, o), (at, px, m_i) in manual.items() if nxt >= at and n > m_i}
    left = [p for p in res['positions'] if (p['inst'], int(p['opened'])) not in books_next]
    held = {g['id']: sum(1 for p in left if p['seg'] == g['id']) for g in acc['segs']}
    held_start = dict(held)
    queue, seen = [], {p['inst'] for p in left}
    sides = {d: sum(1 for p in left if p['side'] == d) for d in (1, -1)}
    for sid, inst, d, dist, b in sorted(res['pending'], key=lambda x: not x[4]):
        if inst in seen or inst in blocked_next or held[sid] >= MAX_POSITIONS or res['halted'] or same_side_full(sides[d], ACCOUNT_RULES['max_same_side']):
            continue
        seen.add(inst); held[sid] += 1; sides[d] += 1; queue.append((sid, inst, d, dist, b))
    pending = [dict(inst=inst, side=d, champion=who(sid)) for sid, inst, d, dist, b in queue]
    # risk factor of the queued entries (fill at the next open), the same rule the next replay applies to them
    seg_by = {x['id']: x for x in segs}
    groups = same_bar_groups(sorted(res['pending'], key=lambda x: not x[4]), {p['inst'] for p in left},
                             {g['id']: sum(1 for p in left if p['seg'] == g['id']) for g in acc['segs']},
                             eligible=lambda k: k not in blocked_next)

    # PRICE_OFF LIVE: the live follower's groups for the next open (paper + virtual entries together, execution.
    # live_same_bar_groups, as the next replay counts them): size the queued price_off_live entries and give the queued
    # paper entries their `live_risk_factor`; empty while the cutover key is pending or before it
    lgroups = live_same_bar_groups(sorted(res['pending'], key=lambda x: not x[4]),
                                   {p['inst'] for p in left} | {p['inst'] for p in res['off_book']},
                                   {g['id']: sum(1 for p in left if p['seg'] == g['id']) for g in acc['segs']},
                                   {g['id']: sum(1 for p in res['off_book'] if p['seg'] == g['id']) for g in acc['segs']},
                                   lambda k: k in blocked_next) \
        if 'off_book' in res and s.n > ci['price_off_live_from'] and not res['halted'] else None

    def queue_factor(sid, d, grp=None):
        fb = res.get('first_fill', {}).get(seg_by[sid]['chain'])
        return entry_risk_factor((groups if grp is None else grp).get((sid, d), 1), seat_age_h=(last_ts + HOUR - seg_by[sid]['tenure_ts']) / HOUR,
                                 first=fb is None or fb == s.n, winding=False)

    def live_factor(sid, d, factor):
        """{'live_risk_factor': x} for a queued paper entry whose live share is smaller than its paper one, else {}."""
        if lgroups is None:
            return {}
        x = min(factor, queue_factor(sid, d, lgroups))
        return {'live_risk_factor': round(x, 6)} if round(x, 6) != round(factor, 6) else {}
    members = []
    for g in acc['segs']:
        if g['wind_ts'] is None:
            since = next((m['since'] for m in team if m['id'] == g['champion']), None)
            pnl = res['pnl'][g['id']]
            members.append(dict(slot=g['slot'], id=g['champion'], species=g['species'], since=since, rank=rank.get(g['champion'], 99) + 1,
                                pnl=round(pnl, 2), ret=round(pnl / acc['start_equity'] * 100, 3),
                                positions=sum(1 for p in res['positions'] if p['seg'] == g['id']), trades=sum(1 for t in res['trades'] if t['seg'] == g['id']),
                                boost=bool(g['boosts']) and g['boosts'][-1][1] is None,
                                entry_gate=entry_gate(nodes[g['champion']], state=state),
                                trading=g['wind_ts'] is None and g['from_ts'] <= last_ts and allowed(g['champion']),
                                **({'effective_from': g['from_ts']} if g['from_ts'] > last_ts else {})))   # seated from a pending bar
    # Mirror signals: one per coin (the account holds one position per coin). 'rank' orders the mirror's entries.
    signals = {}
    for p in res['positions']:
        signals[p['inst']] = dict(side=p['side'], entry=p['entry'], stop=p['stop'], tp=p['target'], dist=p['dist'], trailing=p['trailing'],
                                  opened=p['opened'], strategy='evolved',
                                  champion=who(p['seg']), rank=rank.get(who(p['seg']), 99) + 1,
                                  risk_factor=round(p.get('risk_factor', 1.0), 6), seat_from=seg_by[p['seg']]['tenure_ts'],
                                  winding=wound(by[p['seg']]),
                                  **({'live_risk_factor': round(p['live_risk_factor'], 6)} if 'live_risk_factor' in p else {}),
                                  **_size(by[p['seg']]['genes'], leader=p['boost']))   # the base risk: the mirror applies the majors / crowding steps itself
    for sid, inst, d, dist, b in queue:
        g = by[sid]
        signals.setdefault(inst, dict(_signal_from_pending(inst, d, g['genes'], series, last_ts, g['champion']), rank=rank.get(g['champion'], 99) + 1,
                                      risk_factor=round(queue_factor(sid, d), 6), seat_from=seg_by[sid]['tenure_ts'], winding=False,
                                      **live_factor(sid, d, queue_factor(sid, d)), **_size(g['genes'], leader=b)))
    price_off_live, price_off_live_exits = _price_off_live(res, segs, by, seg_by, series, last_ts, left, held_start, blocked_next, rank, who, wound,
                                                           ci['price_off_live_from'], lgroups or {})
    legacy = paper.get('legacy') or {}
    seats = [dict(champion=g['champion'], slot=g['slot'], species=g['species'], reason=g['reason'], from_ts=g['from_ts'], to_ts=g['wind_ts']) for g in acc['segs']]
    seats += [dict(champion=g['champion'], slot=g['slot'], species=g['species'], reason='leader', from_ts=a, to_ts=b) for g in acc['segs'] for a, b in g['boosts']]
    seats.sort(key=lambda x: (x['from_ts'], x['reason'] == 'leader'), reverse=True)
    equity = round(res['equity'], 2)
    view = dict(equity=equity, start_equity=paper['start_equity'], ret=round((equity / paper['start_equity'] - 1) * 100, 3),
                price_off=sorted(off_now), mode='cross', account_from=acc['from_ts'], account_start=acc['start_equity'], halted=res['halted'],
                # cross liquidations of the shared account, and insurance-fund compensation when a gap passed bankruptcy:
                # account_start + Σ members' pnl (incl. wound-down seats) + insurance = equity
                liquidations=res.get('liquidations', 0), insurance=round(res.get('insurance', 0.0), 2),
                adjustments=[dict(a, amount=round(a['amount'], 2)) for a in res.get('adjustments') or []][-20:],
                rules=dict(reverse_close=ACCOUNT_RULES['reverse_close'], exit_rules_from=acc.get('exit_rules_from'),
                           trail_risk_from=acc.get('trail_risk_from'), next_hour_from=acc.get('next_hour_from'), trail=dict(ACCOUNT_RULES['trail']),
                           same_bar_share=ACCOUNT_RULES['same_bar_share'], new_seat_h=ACCOUNT_RULES['new_seat_h'],
                           new_seat_factor=ACCOUNT_RULES['new_seat_factor'],
                           same_side_cap=ACCOUNT_RULES['same_side_cap'], max_same_side=ACCOUNT_RULES['max_same_side'], daily_halt=ACCOUNT_RULES['daily_halt'], lever=ACCOUNT_RULES['lever'],
                           leader_risk=ACCOUNT_RULES['leader_risk'], leader_lever=ACCOUNT_RULES['leader_lever'],
                           max_risk=ACCOUNT_RULES['max_risk'], min_risk=ACCOUNT_RULES['min_risk'],
                           min_lever=ACCOUNT_RULES.get('min_lever', 1), margin_mode=ACCOUNT_RULES.get('margin_mode', 'cross'),
                           majors=list(ACCOUNT_RULES['majors']), major_factor=ACCOUNT_RULES['major_factor']),
                curve=([p for p in legacy.get('curve') or [] if p[0] < acc['from_ts']] + [list(p) for p in res['curve']])[-400:],
                # Recent trades and their counts are the current shared account only.
                # paper['legacy']['trades'] (old 5-slot book, including 'switch' closes before
                # 2026-11-11T04:00Z) stay in state for history but are not published on the dashboard.
                trades=sorted(trades, key=lambda t: t['closed'], reverse=True)[:50],
                positions=positions, pending=pending,
                leader=_leader_view(leader, nodes) if leader else None,
                members=sorted(members, key=lambda m: m['rank']), segments=(seats + (legacy.get('segments') or []))[:30],
                # changes stamped on a bar after the last one (NEXT-HOUR RULE): in effect once a replay has that bar
                scheduled=scheduled(acc, price_off, manual_stamps=mfrom, last_ts=last_ts),
                # recent closes and why ({inst|opened: reason}); run.py moves this into signals.json for the OKX follow
                # (a manual close counts from the bar it was booked on, its `closed` being the real, possibly older, time)
                exits={f"{t['inst']}|{int(t['opened'])}": t['reason'] for t in res['trades'] if t.get('booked', t['closed']) >= last_ts - EXIT_WINDOW},
                # PRICE_OFF LIVE (_price_off_live): run.py / signals_hourly.py move both into signals.json; the OKX LIVE follower
                # alone follows them (demo keeps its own demo_price_off check and ignores them)
                price_off_live=price_off_live, price_off_live_exits=price_off_live_exits)
    return view, signals


def _price_off_live(res, segs, by, seg_by, series, last_ts, left, held_start, blocked_next, rank, who, wound, from_i, groups):
    """PRICE_OFF LIVE (2026-11-25, user: 「先改过滤，让它不再挡实盘」). price_off compares the OKX DEMO market with the real one;
    the paper account keeps refusing those coins (unchanged), but the live account trades the real market, so the
    qualified signals refused ONLY by price_off are published separately for the OKX live follower:
    ({inst: signal}, {inst|opened: exit reason}).
    - open virtual positions (execution.simulate_account off_book: filled after account['price_off_live_from'], never in
      the paper account) and entries queued for the next open that only price_off refuses (blocked_next), checked in the
      same order and with the same tests as the paper queue above;
    - each signal has the fields of a paper signal (side, entry, stop, tp, dist, trailing, opened, champion, rank,
      risk / leverage genes, risk_factor, seat_from, winding) plus price_off=True and the genes the live follower needs to
      recompute its own stop / take-profit from real OKX candles at its own entry: tf, stop_atr, target_rr. entry / stop /
      tp here are the paper's (real-market candles), for late_ok and the record only;
    - exits: virtual closes in the last EXIT_WINDOW (stop / target / trailing_stop / reverse_signal), so the follower
      closes its position when the strategy's own exit fires (the same way a paper close closes a mirror).
    - risk_factor: TRAIL & RISK RULES b counted over the paper AND the price_off entries of the seat / bar / side
      together (`groups` = execution.live_same_bar_groups for the next open; simulate_account for the filled ones), since
      the live follower opens both; the paper signals of such a bar carry the matching `live_risk_factor`.
    Empty while the cutover key is pending (a replay of a state that does not have it yet)."""
    if 'off_book' not in res:
        return {}, {}
    out, n = {}, next(iter(series.values())).n

    def extra(genes):
        g = fix(dict(genes))
        return dict(price_off=True, tf=int(g.get('tf', 1)), stop_atr=g['stop_atr'], target_rr=target_rr(g))
    for p in res['off_book']:
        g = by[p['seg']]
        out[p['inst']] = dict(side=p['side'], entry=p['entry'], stop=p['stop'], tp=p['target'], dist=p['dist'], trailing=p['trailing'],
                              opened=p['opened'], strategy='evolved', champion=who(p['seg']), rank=rank.get(who(p['seg']), 99) + 1,
                              risk_factor=round(p.get('risk_factor', 1.0), 6), seat_from=seg_by[p['seg']]['tenure_ts'], winding=wound(g),
                              **_size(g['genes'], leader=p['boost']), **extra(g['genes']))
    if n > from_i and not res['halted']:          # queued for the next open (bar n): only when the next fill is in the new era
        held, vheld = dict(held_start), {x['id']: sum(1 for p in res['off_book'] if p['seg'] == x['id']) for x in segs}
        seen, sides = {p['inst'] for p in left}, {d: sum(1 for p in left if p['side'] == d) for d in (1, -1)}
        # max_same_side: the paper queue counts the paper positions only (sides, as in update()); a virtual entry counts
        # what the live follower holds, paper AND virtual (live_sides, as simulate_account's off_book_entry)
        live_sides = {d: sides[d] + sum(1 for p in res['off_book'] if p['side'] == d) for d in (1, -1)}
        order = sorted(res['pending'], key=lambda x: not x[4])
        for sid, inst, d, dist, b in order:
            if inst in seen or held[sid] >= MAX_POSITIONS:
                continue
            if inst in blocked_next:                  # the paper queue skips it; only price_off refuses it
                if dist > 0 and inst not in out and held[sid] + vheld[sid] < MAX_POSITIONS \
                        and not same_side_full(live_sides[d], ACCOUNT_RULES['max_same_side']):
                    g = by[sid]
                    fb = res.get('first_fill', {}).get(seg_by[sid]['chain'])
                    factor = entry_risk_factor(groups.get((sid, d), 1), seat_age_h=(last_ts + HOUR - seg_by[sid]['tenure_ts']) / HOUR,
                                               first=fb is None or fb == n, winding=False)
                    out[inst] = dict(_signal_from_pending(inst, d, g['genes'], series, last_ts, g['champion']), rank=rank.get(g['champion'], 99) + 1,
                                     risk_factor=round(factor, 6), seat_from=seg_by[sid]['tenure_ts'], winding=False,
                                     **_size(g['genes'], leader=b), **extra(g['genes']))
                    vheld[sid] += 1; live_sides[d] += 1
                continue
            if same_side_full(sides[d], ACCOUNT_RULES['max_same_side']):
                continue
            seen.add(inst); held[sid] += 1; sides[d] += 1; live_sides[d] += 1   # the paper queue's own entry (same tests as above)
    exits = {f"{t['inst']}|{int(t['opened'])}": t['reason'] for t in res['off_book_trades'] if t['closed'] >= last_ts - EXIT_WINDOW}
    return out, exits

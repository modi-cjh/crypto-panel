"""Paper trading for the champion team: ONE shared account, cross margin (全仓), run by execution.simulate_account().

The team's strategies all trade the same account: every position is sized on the whole account's equity (risk gene × equity;
the leader: 5 %), margin is shared, one position per coin, and the same-side cap and the daily halt are the OKX mirror's
(tools/okx_demo_trader.py). Each run replays the account from its start bar with the full seat history, so the result is
exactly what the engine produces on live bars:
  account = dict(from_ts, start_equity, segs=[dict(id, champion, slot, genes, species, from_ts, wind_ts, boosts, reason)])
- a member that loses its seat gets wind_ts: no new signals, its open positions run to their own exits;
- `boosts` are the [from_ts, to_ts] ranges in which the member held the bigger size (state['leader']: the best member that
  passed both the out-of-sample and the final gate; nobody when no member qualifies).
Before this, the account was five separate slots of 1/5 each; that history (curve, trades, seat changes) is kept as
paper['legacy'] and the shared account starts from the slots' total equity. The slot code below `_legacy_update` only
serves that one-time conversion.
"""
from execution import simulate, simulate_account, stop_distance, START_EQUITY, ACCOUNT_RULES, MAX_POSITIONS
from evolution import TEAM_SIZE, entry_gate

HOUR = 3600_000
LEADER_RISK, LEADER_LEVERAGE = ACCOUNT_RULES['leader_risk'], ACCOUNT_RULES['leader_lever']
_CTX = dict(extra={})        # per update(): {slot index: {ts: equity of the other slots}} for boosted segments


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
    boost = dict(risk=LEADER_RISK, lever_cap=LEADER_LEVERAGE, extra_equity=_CTX['extra'].get(seg.get('slot'), {})) if seg.get('boost') else {}
    return simulate(genes, series, start, s.n, equity=seg['start_equity'], close_at_end=close_at_end, curve_every=1,
                    stop_entries_at=_index(series, wind_ts) if wind_ts is not None else None, **boost)


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
    dist, px, rr = stop_distance(genes, sr, sr.n - 1), sr.c[-1], genes['target_rr']
    return dict(side=d, entry=px, stop=px - d * dist, tp=px + d * rr * dist if rr > 0 else None, opened=last_ts + HOUR,
                strategy='evolved', leverage=10, champion=member, pending=True)


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
        slot['segments'].append(dict(champion=new, slot=slot_i, genes=node['genes'], species=node['species'], from_ts=last_ts, reason=reason,
                                     start_equity=round(equity, 2), to_ts=None, end_equity=None, boost=new == leader))
    for slot_i, m in enumerate(team):        # a seated member whose slot has no running segment (e.g. after migration) starts one
        slot = slots[slot_i]
        if not (slot['segments'] and slot['segments'][-1]['to_ts'] is None and slot['segments'][-1]['champion'] == m['id']):
            equity = _retire(slot, series, last_ts) if slot['segments'] and slot['segments'][-1]['to_ts'] is None else _idle_equity(slot)
            node = nodes[m['id']]
            slot['segments'].append(dict(champion=m['id'], slot=slot_i, genes=node['genes'], species=node['species'], from_ts=last_ts,
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
        size = dict(risk=LEADER_RISK, leverage=LEADER_LEVERAGE, leader=True) if seg.get('boost') else dict(risk=seg['genes']['risk'], leverage=10)
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
                leader=dict(id=leader, risk=LEADER_RISK, leverage=LEADER_LEVERAGE) if leader else None,
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


def update(state, series, changes, price_off=None):
    """price_off: data.price_check ranges {inst: [[from_ts, to_ts or None]]} — coins that take no new entry in those ranges.
    Apply this run's seat and leader changes, replay the shared account, and return (dashboard view, mirror signals)."""
    nodes, s = state['nodes'], next(iter(series.values()))
    last_ts = s.ts[-1]
    paper = state.get('paper') or {}
    if 'account' not in paper:
        paper = _convert(state, series, last_ts)
    acc, team = paper['account'], state.get('team', [])
    # Only members who pass the three-part entry gate may keep or take a live paper seat / OKX follow.
    # Failing members are wound down (open positions run to their own exits). If nobody passes, the
    # account holds cash — it never falls back to an ungated strategy. Existing open size is not resized.
    allowed = lambda nid: bool(nid) and nid in nodes and entry_gate(nodes[nid], state=state)['passed']
    leader = state.get('leader') if allowed(state.get('leader')) else None
    seated = lambda slot_i: next((g for g in acc['segs'] if g['slot'] == slot_i and g['wind_ts'] is None), None)

    def wind(g):
        g['wind_ts'] = last_ts
        if g['boosts'] and g['boosts'][-1][1] is None:
            g['boosts'][-1][1] = last_ts

    def seat(slot_i, nid, reason):
        cur = seated(slot_i)
        if cur:
            wind(cur)
        acc['next'] += 1
        acc['segs'].append(dict(id=f"a{acc['next']}", champion=nid, slot=slot_i, genes=nodes[nid]['genes'], species=nodes[nid]['species'],
                                from_ts=last_ts, wind_ts=None, boosts=[], reason=reason))

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
                g['boosts'].append([last_ts, None])
            elif g['champion'] != leader and is_leader:
                g['boosts'][-1][1] = last_ts
    idx = lambda ts: _index(series, ts)
    segs = [dict(id=g['id'], genes=g['genes'], from_i=idx(g['from_ts']), wind_i=idx(g['wind_ts']) if g['wind_ts'] is not None else None,
                 boosts=[(idx(a), idx(b) if b is not None else None) for a, b in g['boosts']]) for g in acc['segs']]
    blocked = {inst: [(idx(a), idx(b) if b is not None else None) for a, b in rs] for inst, rs in (price_off or {}).items()}
    off_now = {inst for inst, rs in (price_off or {}).items() if rs and rs[-1][1] is None}
    res = simulate_account(segs, series, idx(acc['from_ts']), s.n, equity=acc['start_equity'], blocked=blocked)
    by = {g['id']: g for g in acc['segs']}
    rank = {m['id']: r for r, m in enumerate(sorted(team, key=lambda m: (m['id'] != leader, -nodes[m['id']]['fitness'])))}   # the leader is rank 1
    who = lambda sid: by[sid]['champion']
    tag = lambda x: dict({k: v for k, v in x.items() if k != 'seg'}, champion=who(x['seg']), slot=by[x['seg']]['slot'])
    positions = [dict(tag(p), **({'winding': True} if by[p['seg']]['wind_ts'] is not None else {})) for p in res['positions']]
    trades = [tag(t) for t in res['trades']]
    # Signals waiting for the next open, as far as the account will take them: one per coin, the leader's first, and no more
    # than a strategy's free position slots (otherwise the mirror would open entries the account itself never fills).
    held = {g['id']: sum(1 for p in res['positions'] if p['seg'] == g['id']) for g in acc['segs']}
    queue, seen = [], {p['inst'] for p in res['positions']}
    for sid, inst, d, dist, b in sorted(res['pending'], key=lambda x: not x[4]):
        if inst in seen or inst in off_now or held[sid] >= MAX_POSITIONS or res['halted']:
            continue
        seen.add(inst); held[sid] += 1; queue.append((sid, inst, d, dist, b))
    pending = [dict(inst=inst, side=d, champion=who(sid)) for sid, inst, d, dist, b in queue]
    members = []
    for g in acc['segs']:
        if g['wind_ts'] is None:
            since = next((m['since'] for m in team if m['id'] == g['champion']), None)
            pnl = res['pnl'][g['id']]
            members.append(dict(slot=g['slot'], id=g['champion'], species=g['species'], since=since, rank=rank.get(g['champion'], 99) + 1,
                                pnl=round(pnl, 2), ret=round(pnl / acc['start_equity'] * 100, 3),
                                positions=sum(1 for p in res['positions'] if p['seg'] == g['id']), trades=sum(1 for t in res['trades'] if t['seg'] == g['id']),
                                boost=bool(g['boosts']) and g['boosts'][-1][1] is None,
                                entry_gate=entry_gate(nodes[g['champion']], state=state), trading=g['wind_ts'] is None and allowed(g['champion'])))
    # Mirror signals: one per coin (the account holds one position per coin). 'rank' orders the mirror's entries.
    signals = {}
    size = lambda boost, risk: dict(risk=LEADER_RISK, leverage=LEADER_LEVERAGE, leader=True) if boost else dict(risk=risk, leverage=ACCOUNT_RULES['lever'])
    for p in res['positions']:
        signals[p['inst']] = dict(side=p['side'], entry=p['entry'], stop=p['stop'], tp=p['target'], opened=p['opened'], strategy='evolved',
                                  champion=who(p['seg']), rank=rank.get(who(p['seg']), 99) + 1, **size(p['boost'], by[p['seg']]['genes']['risk']))   # the base risk: the mirror applies the majors / crowding steps itself
    for sid, inst, d, dist, b in queue:
        g = by[sid]
        signals.setdefault(inst, dict(_signal_from_pending(inst, d, g['genes'], series, last_ts, g['champion']), rank=rank.get(g['champion'], 99) + 1,
                                      **size(b, g['genes']['risk'])))
    legacy = paper.get('legacy') or {}
    seats = [dict(champion=g['champion'], slot=g['slot'], species=g['species'], reason=g['reason'], from_ts=g['from_ts'], to_ts=g['wind_ts']) for g in acc['segs']]
    seats += [dict(champion=g['champion'], slot=g['slot'], species=g['species'], reason='leader', from_ts=a, to_ts=b) for g in acc['segs'] for a, b in g['boosts']]
    seats.sort(key=lambda x: (x['from_ts'], x['reason'] == 'leader'), reverse=True)
    equity = round(res['equity'], 2)
    view = dict(equity=equity, start_equity=paper['start_equity'], ret=round((equity / paper['start_equity'] - 1) * 100, 3),
                price_off=sorted(off_now), mode='cross', account_from=acc['from_ts'], account_start=acc['start_equity'], halted=res['halted'],
                rules=dict(same_side_cap=ACCOUNT_RULES['same_side_cap'], daily_halt=ACCOUNT_RULES['daily_halt'], lever=ACCOUNT_RULES['lever'],
                           leader_risk=ACCOUNT_RULES['leader_risk'], leader_lever=ACCOUNT_RULES['leader_lever'],
                           max_risk=ACCOUNT_RULES['max_risk'], majors=list(ACCOUNT_RULES['majors']), major_factor=ACCOUNT_RULES['major_factor']),
                curve=([p for p in legacy.get('curve') or [] if p[0] < acc['from_ts']] + [list(p) for p in res['curve']])[-400:],
                trades=sorted(trades + (legacy.get('trades') or []), key=lambda t: t['closed'], reverse=True)[:50],
                positions=positions, pending=pending,
                leader=dict(id=leader, risk=LEADER_RISK, leverage=LEADER_LEVERAGE) if leader else None,
                members=sorted(members, key=lambda m: m['rank']), segments=(seats + (legacy.get('segments') or []))[:30])
    return view, signals

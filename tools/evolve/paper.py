"""Paper trading for the champion team, using the SAME simulate() as the backtests (同源执行).

The account is split into TEAM_SIZE equal slots. Each slot is a chain of segments, one per member that held the seat;
each run replays every slot's current segment from the bar it started on up to the latest closed bar, so the paper
result is exactly what the backtest engine produces on live bars. When a seat changes hands, the old member's segment is
closed at that bar's close and the new member continues with the slot's equity. The account equity is the slots' sum.
"""
from execution import simulate, stop_distance, START_EQUITY
from evolution import TEAM_SIZE

HOUR = 3600_000


def _index(series, ts):
    s = next(iter(series.values()))
    for i in range(s.n - 1, -1, -1):
        if s.ts[i] <= ts:
            return i
    return 0


def run_segment(seg, genes, series, close_at_end=False):
    s = next(iter(series.values()))
    start = _index(series, seg['from_ts'])
    if start >= s.n:
        return dict(equity=seg['start_equity'], trades=[], curve=[(seg['from_ts'], seg['start_equity'])], positions=[], pending={}, metrics=None)
    return simulate(genes, series, start, s.n, equity=seg['start_equity'], close_at_end=close_at_end, curve_every=1)


def _new_slot(equity):
    return dict(start_equity=round(equity, 2), segments=[], closed_trades=[], curve_done=[])


def _migrate(paper, series):
    """Single-champion paper account → team slots. The old account's equity is split equally across the slots."""
    if 'slots' in paper:
        return paper
    equity = paper.get('start_equity', START_EQUITY)
    if paper.get('segments'):
        seg = paper['segments'][-1]
        equity = run_segment(seg, seg['genes'], series, close_at_end=True)['equity']
    legacy = [{k: v for k, v in sg.items() if k != 'genes'} for sg in paper.get('segments', [])]
    return dict(start_equity=paper.get('start_equity', START_EQUITY), slots=[_new_slot(equity / TEAM_SIZE) for _ in range(TEAM_SIZE)],
                legacy_segments=legacy, legacy_trades=paper.get('closed_trades', []))


def _close(slot, series, last_ts):
    seg = slot['segments'][-1]
    res = run_segment(seg, seg['genes'], series, close_at_end=True)
    seg.update(to_ts=last_ts, end_equity=round(res['equity'], 2), trades=len(res['trades']))
    slot['closed_trades'] = (slot['closed_trades'] + [dict(t, champion=seg['champion']) for t in res['trades']])[-300:]
    slot['curve_done'] = (slot['curve_done'] + [list(p) for p in res['curve']])[-1500:]
    return res['equity']


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


def update(state, series, changes):
    """Apply this run's seat changes, advance every slot, and return (dashboard view, mirror signals)."""
    nodes, s = state['nodes'], next(iter(series.values()))
    last_ts = s.ts[-1]
    paper = state['paper'] = _migrate(state.get('paper') or dict(start_equity=START_EQUITY), series)
    slots, team = paper['slots'], state.get('team', [])
    while len(slots) < TEAM_SIZE:
        slots.append(_new_slot(paper['start_equity'] / TEAM_SIZE))
    for slot_i, old, new, reason in changes:
        slot = slots[slot_i]
        equity = _close(slot, series, last_ts) if slot['segments'] and slot['segments'][-1]['to_ts'] is None else (
            slot['segments'][-1]['end_equity'] if slot['segments'] else slot['start_equity'])
        node = nodes[new]
        slot['segments'].append(dict(champion=new, slot=slot_i, genes=node['genes'], species=node['species'], from_ts=last_ts, reason=reason,
                                     start_equity=round(equity, 2), to_ts=None, end_equity=None))
    for slot_i, m in enumerate(team):        # a seated member whose slot has no running segment (e.g. after migration) starts one
        slot = slots[slot_i]
        if not (slot['segments'] and slot['segments'][-1]['to_ts'] is None and slot['segments'][-1]['champion'] == m['id']):
            if slot['segments'] and slot['segments'][-1]['to_ts'] is None:
                _close(slot, series, last_ts)
            equity = slot['segments'][-1]['end_equity'] if slot['segments'] else slot['start_equity']
            node = nodes[m['id']]
            slot['segments'].append(dict(champion=m['id'], slot=slot_i, genes=node['genes'], species=node['species'], from_ts=last_ts,
                                         reason='carry', start_equity=round(equity, 2), to_ts=None, end_equity=None))
    rank = {m['id']: r for r, m in enumerate(sorted(team, key=lambda m: nodes[m['id']]['fitness'], reverse=True))}
    members, curves, trades, positions, pending, signals = [], [], [], [], [], {}
    order = []
    for slot_i, slot in enumerate(slots):
        seg = slot['segments'][-1] if slot['segments'] and slot['segments'][-1]['to_ts'] is None else None
        if not seg:
            curves.append(slot['curve_done'] or [[last_ts, slot['start_equity']]])
            continue
        res = run_segment(seg, seg['genes'], series)
        member = seg['champion']
        curves.append(slot['curve_done'] + [list(p) for p in res['curve']])
        trades += slot['closed_trades'] + [dict(t, champion=member) for t in res['trades']]
        positions += [dict(p, champion=member, slot=slot_i) for p in res['positions']]
        pending += [dict(inst=k, side=v, champion=member) for k, v in (res.get('pending') or {}).items()]
        since = next((m['since'] for m in team if m['id'] == member), None)
        members.append(dict(slot=slot_i, id=member, species=seg['species'], since=since, rank=rank.get(member, 99) + 1,
                            start_equity=slot['start_equity'], equity=round(res['equity'], 2),
                            ret=round((res['equity'] / slot['start_equity'] - 1) * 100, 3),
                            positions=len(res['positions']), trades=len(slot['closed_trades']) + len(res['trades'])))
        order.append((rank.get(member, 99), seg, res))
    # Mirror signals: one per coin; when several members hold the same coin, the higher-ranked member wins.
    # 'rank' (1 = leader) also orders the OKX mirror's entries when its same-side risk cap fills up.
    for r, seg, res in sorted(order, key=lambda x: x[0]):
        member = seg['champion']
        for p in res['positions']:
            signals.setdefault(p['inst'], dict(side=p['side'], entry=p['entry'], stop=p['stop'], tp=p['target'], opened=p['opened'],
                                               strategy='evolved', leverage=10, champion=member, rank=r + 1, risk=seg['genes']['risk']))
        for inst, d in (res.get('pending') or {}).items():
            signals.setdefault(inst, dict(_signal_from_pending(inst, d, seg['genes'], series, last_ts, member), rank=r + 1, risk=seg['genes']['risk']))
    curve = _sum_curves(curves, [sl['start_equity'] for sl in slots])
    equity = round(sum(m['equity'] for m in members) + sum(sl['segments'][-1]['end_equity'] if sl['segments'] and sl['segments'][-1]['to_ts'] else
                                                          (sl['start_equity'] if not sl['segments'] else 0) for sl in slots), 2)
    segments = sorted(({**{k: v for k, v in sg.items() if k != 'genes'}} for sl in slots for sg in sl['segments']), key=lambda x: x['from_ts'], reverse=True)
    trades = sorted(trades + paper.get('legacy_trades', []), key=lambda t: t['closed'], reverse=True)
    view = dict(equity=equity, start_equity=paper['start_equity'], ret=round((equity / paper['start_equity'] - 1) * 100, 3),
                curve=curve[-400:], trades=trades[:50], positions=positions, pending=pending,
                members=sorted(members, key=lambda m: m['rank']), segments=(segments + paper.get('legacy_segments', [])[::-1])[:30])
    return view, signals

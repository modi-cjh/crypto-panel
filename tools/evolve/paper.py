"""Paper trading for the champion, using the SAME simulate() as the backtests (同源执行).

The paper account is a chain of segments, one per champion. Each run replays the current segment from the bar it
started on up to the latest closed bar, so the paper result is exactly what the backtest engine produces on live bars.
When the champion changes, the old segment is closed at that bar's close and the new champion continues with its equity.
"""
from execution import simulate, START_EQUITY

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


def update(state, series, champion_id, switched):
    """Advance the paper account; returns the paper view for the dashboard and the mirror signals."""
    nodes, s = state['nodes'], next(iter(series.values()))
    paper = state.setdefault('paper', dict(start_equity=START_EQUITY, segments=[], closed_trades=[]))
    last_ts = s.ts[-1]
    if champion_id and (not paper['segments'] or switched):
        if paper['segments']:
            seg = paper['segments'][-1]
            res = run_segment(seg, seg['genes'], series, close_at_end=True)
            seg.update(to_ts=last_ts, end_equity=round(res['equity'], 2), trades=len(res['trades']))
            paper['closed_trades'] = (paper['closed_trades'] + [dict(t, champion=seg['champion']) for t in res['trades']])[-500:]
            paper['curve_done'] = (paper.get('curve_done', []) + [list(p) for p in res['curve']])[-2000:]
            equity = res['equity']
        else:
            equity = paper['start_equity']
        node = nodes[champion_id]
        paper['segments'].append(dict(champion=champion_id, genes=node['genes'], species=node['species'], from_ts=last_ts,
                                      start_equity=round(equity, 2), to_ts=None, end_equity=None))
    if not paper['segments']:
        return dict(equity=paper['start_equity'], start_equity=paper['start_equity'], curve=[], trades=[], positions=[], segments=[]), {}
    seg = paper['segments'][-1]
    res = run_segment(seg, seg['genes'], series)
    trades = paper['closed_trades'] + [dict(t, champion=seg['champion']) for t in res['trades']]
    curve = paper.get('curve_done', []) + [list(p) for p in res['curve']]
    # Mirror signals: open paper positions, plus entries decided on the last close that fill at the next open.
    signals = {}
    for p in res['positions']:
        signals[p['inst']] = dict(side=p['side'], entry=p['entry'], stop=p['stop'], tp=p['target'], opened=p['opened'], strategy='evolved',
                                  leverage=10, champion=seg['champion'])
    for inst, d in (res.get('pending') or {}).items():
        sr = series[inst]
        dist = seg['genes']['stop_atr'] * sr.atr()[-1]
        px = sr.c[-1]
        rr = seg['genes']['target_rr']
        signals[inst] = dict(side=d, entry=px, stop=px - d * dist, tp=px + d * rr * dist if rr > 0 else None, opened=last_ts + HOUR,
                             strategy='evolved', leverage=10, champion=seg['champion'], pending=True)
    view = dict(equity=round(res['equity'], 2), start_equity=paper['start_equity'], curve=curve[-400:], trades=trades[-50:][::-1],
                positions=[dict(p, champion=seg['champion']) for p in res['positions']],
                pending=[dict(inst=k, side=v) for k, v in (res.get('pending') or {}).items()],
                segments=[{k: v for k, v in sg.items() if k != 'genes'} for sg in paper['segments']][-20:],
                ret=round((res['equity'] / paper['start_equity'] - 1) * 100, 3))
    return view, signals

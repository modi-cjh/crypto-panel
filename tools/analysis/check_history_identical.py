#!/usr/bin/env python3
"""Check that a code version replays the published paper history byte-identically: run paper.update (as run.py, no
evolution) of <repo> on a paper-data snapshot and a candle cache, and dump the trades / curve / positions. Compare two
dumps with --compare a.json b.json <prev_last ms>: every trade closed (booked) at or before prev_last and every curve
point at or before prev_last must be identical.
usage: check_history_identical.py dump <repo> <paper dir> <candle cache> <out.json>
       check_history_identical.py compare <a.json> <b.json>"""
import copy
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def dump(repo, paper_dir, cache, out):
    sys.path.insert(0, str(Path(repo) / 'tools' / 'evolve'))
    import execution as ex
    import paper as P
    paper_dir, cache = Path(paper_dir), Path(cache)
    state = json.loads((paper_dir / 'state.json').read_text())
    uni = json.loads((paper_dir / 'universe.json').read_text())
    prev_last = int(json.loads((paper_dir / 'dashboard.json').read_text())['paper']['curve'][-1][0])
    H = 3600_000
    last = json.loads((cache / 'BTC-USDT-SWAP.json').read_text())['rows'][-1][0]
    t0 = last + H - 365 * 24 * H
    per, fund = {}, {}
    for k in uni['instruments']:
        rows = [r for r in json.loads((cache / f'{k}.json').read_text())['rows'] if t0 <= r[0] <= last]
        if rows:
            per[k] = rows
            fp = cache / 'funding' / f'{k}.json'
            fund[k] = json.loads(fp.read_text())['rows'] if fp.exists() else []
    newest = max(r[-1][0] for r in per.values())
    per = {k: v for k, v in per.items() if v[-1][0] >= newest - 3 * H}
    keep = set.intersection(*(set(r[0] for r in v) for v in per.values()))
    series = {}
    for k, rows in per.items():
        rows = [r for r in rows if r[0] in keep]; ts = [r[0] for r in rows]
        known = dict(fund[k]); first = fund[k][0][0] if fund[k] else float('inf')
        f = {t: .0001 for t in ts if t < first and t % (8 * H) == 0}; f.update({t: known[t] for t in ts if t in known})
        series[k] = ex.Series(k, ts, [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows],
                              spec=tuple(uni['specs'][k]) if k in uni.get('specs', {}) else None, funding=f)
    for k, s in series.items():
        if k != 'BTC-USDT-SWAP':
            s.ref = series['BTC-USDT-SWAP']
    st = copy.deepcopy(state)
    captured = {}
    orig = P.simulate_account

    def spy(*a, **kw):
        captured['res'] = orig(*a, **kw)
        return captured['res']
    P.simulate_account = spy
    view, signals = P.update(st, series, [], uni.get('price_off', {}), prev_last=prev_last)
    res = captured['res']
    Path(out).write_text(json.dumps(dict(prev_last=prev_last, last_bar=next(iter(series.values())).ts[-1], trades=res['trades'], curve=res['curve'],
                                         account={k: v for k, v in st['paper']['account'].items() if k != 'segs'}, signals=signals), sort_keys=True, default=str))
    print(out, 'trades', len(res['trades']), 'curve', len(res['curve']), 'equity', view['equity'])


def compare(a, b):
    A, B = json.loads(Path(a).read_text()), json.loads(Path(b).read_text())
    cut = A['prev_last']
    ta = [t for t in A['trades'] if t.get('booked', t['closed']) <= cut]
    tb = [t for t in B['trades'] if t.get('booked', t['closed']) <= cut]
    ca = [p for p in A['curve'] if p[0] <= cut]
    cb = [p for p in B['curve'] if p[0] <= cut]
    same_t = json.dumps(ta, sort_keys=True) == json.dumps(tb, sort_keys=True)
    same_c = json.dumps(ca, sort_keys=True) == json.dumps(cb, sort_keys=True)
    later = lambda X: [t for t in X['trades'] if t.get('booked', t['closed']) > cut]
    print(json.dumps(dict(prev_last=cut, trades_up_to_prev_last=[len(ta), len(tb)], identical_trades=same_t, curve_points=[len(ca), len(cb)],
                          identical_curve=same_c, later_trades=[len(later(A)), len(later(B))]), indent=1))
    return 0 if same_t and same_c else 1


if __name__ == '__main__':
    if sys.argv[1] == 'dump':
        dump(*sys.argv[2:6])
    else:
        sys.exit(compare(*sys.argv[2:4]))

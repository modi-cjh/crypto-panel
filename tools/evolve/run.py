#!/usr/bin/env python3
"""Self-evolving strategies: one run = update data → evolve N generations → pick the champion → paper-trade it.

Outputs in <data>/evolve/:
  state.json      full engine state (population, lineage, hall of fame, paper account)
  dashboard.json  what the dashboard's 策略进化 page reads
  signals.json    the champion's paper positions, in the format tools/okx_demo_trader.py mirrors to OKX demo
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data as D                      # noqa: E402
import evolution as E                 # noqa: E402
import paper as P                     # noqa: E402
from genome import describe, SPECIES_NAMES, GENES  # noqa: E402


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def node_view(n, curves=False):
    keep = ('id', 'gen', 'born', 'died', 'species', 'parents', 'how', 'fitness', 'qualified', 'train', 'test', 'genes')
    v = {k: n[k] for k in keep if k in n}
    v['summary'] = describe(n['genes'])
    v['test_curve'] = n.get('test_curve', [])[:: max(1, len(n.get('test_curve', [])) // 20)]
    if curves:
        v['train_curve'], v['test_curve'] = n.get('train_curve', []), n.get('test_curve', [])
    return v


def dashboard(state, series, split, obs, paper_view, timing):
    s = next(iter(series.values()))
    start, cut, end = split
    nodes, champ = state['nodes'], state['champion']
    return dict(
        schema=1, updated_at=iso(int(time.time() * 1000)), generation=state['generation'], population=state['population'],
        stats=dict(state['stats'], population=len(state['population']), elites=E.ELITES, offspring=E.POPULATION - E.ELITES, hall=len(state['hall'])),
        data=dict(instruments=list(series), bar='1H', bars=s.n, train_from=iso(s.ts[start]), train_to=iso(s.ts[cut - 1]),
                  test_from=iso(s.ts[cut]), test_to=iso(s.ts[end - 1]), train_share=D.TRAIN_SHARE),
        gate=E.GATE, volatility=obs, history=state['history'][-500:], timing=timing,
        species_names=SPECIES_NAMES, gene_bounds={k: [v[1], v[2]] if v[0] != 'choice' else v[1] for k, v in GENES.items()},
        nodes=[node_view(n, curves=n['id'] == champ) for n in nodes.values()],
        hall=state['hall'], champion=node_view(nodes[champ], curves=True) if champ in nodes else None,
        paper=paper_view)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--generations', type=int, default=5)
    ap.add_argument('--budget', type=float, default=420, help='seconds; stop breeding early when exceeded')
    ap.add_argument('--reset', action='store_true')
    args = ap.parse_args(argv)
    t0, now = time.time(), int(time.time() * 1000)
    out = args.data / 'evolve'; out.mkdir(parents=True, exist_ok=True)
    series = D.load_series(args.data, now)
    split = D.split(series)
    state_path = out / 'state.json'
    if args.reset or not state_path.exists():
        state = E.init_state(seed=now % 1_000_000, series=series, split=split)
    else:
        state = json.loads(state_path.read_text())
        E.refresh(state, series, split)
    obs, rows = None, []
    for _ in range(args.generations):
        obs = D.observe_volatility(series)            # observe first, then breed
        rows.append(E.generation(state, series, split, obs))
        if time.time() - t0 > args.budget:
            break
    obs = obs or D.observe_volatility(series)
    champion, switched = E.choose_champion(state)
    paper_view, signals = P.update(state, series, champion, switched)
    E.prune(state)
    timing = dict(seconds=round(time.time() - t0, 1), generations=len(rows), switched=switched)
    state_path.write_text(json.dumps(state, separators=(',', ':')))
    (out / 'dashboard.json').write_text(json.dumps(dashboard(state, series, split, obs, paper_view, timing), ensure_ascii=False, separators=(',', ':')))
    (out / 'signals.json').write_text(json.dumps(dict(updated_at=iso(now), champion=champion, positions=signals), indent=1))
    for r in rows:
        print(f"第 {r['gen']} 代 · 波动 {r['regime']} ({r['vol']}) · 最佳适应度 {r['best']} · 平均 {r['mean']} · 合格 {r['qualified']} · 新生 {r['born']} · 淘汰 {r['killed']}")
    print(f"冠军 {champion}{'（新）' if switched else ''} · 纸面权益 {paper_view['equity']} · 用时 {timing['seconds']}s")


if __name__ == '__main__':
    main()

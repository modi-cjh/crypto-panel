#!/usr/bin/env python3
"""Self-evolving strategies: one run = update data → evolve N generations → pick the champion → paper-trade it.

Outputs in <data>/evolve/:
  state.json      full engine state (population, lineage, hall of fame, paper account)
  dashboard.json  what the dashboard's 策略进化 page reads (lineage nodes without genes / curves, to stay small on phones)
  nodes.json      every node's genes, summary and out-of-sample curve; the page loads it when a node is opened
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


GRAPH_KEYS = ('id', 'gen', 'born', 'died', 'species', 'parents', 'how', 'origin', 'fitness', 'qualified', 'train', 'test', 'final', 'test_ok', 'final_ok')


def node_view(n, curves=False, slim=False):
    v = {k: n[k] for k in GRAPH_KEYS if k in n}
    v['genes'] = n['genes']                    # kept even when slim: pages from before nodes.json require genes on every node
    if slim:                                   # summary and curves live in nodes.json
        return v
    v['summary'] = describe(n['genes'])
    v['test_curve'] = n.get('test_curve', [])[:: max(1, len(n.get('test_curve', [])) // 20)]
    if curves:
        v['train_curve'], v['test_curve'] = n.get('train_curve', []), n.get('test_curve', [])
    return v


def node_details(state):
    keep = ('genes', 'summary', 'test_curve')
    return dict(schema=1, generation=state['generation'], updated_at=iso(int(time.time() * 1000)),
                nodes={nid: {k: v for k, v in node_view(n).items() if k in keep} for nid, n in state['nodes'].items()})


def dashboard(state, series, split, obs, paper_view, timing):
    s = next(iter(series.values()))
    start, cut, mid, end = split
    nodes, champ = state['nodes'], state['champion']
    seated = {m['id'] for m in state.get('team', [])} | {champ}
    return dict(
        schema=1, details='nodes.json', updated_at=iso(int(time.time() * 1000)), generation=state['generation'], population=state['population'],
        stats=dict(state['stats'], population=len(state['population']), elites=E.ELITES, offspring=E.POPULATION - E.ELITES, hall=len(state['hall'])),
        data=dict(instruments=list(series), bar='1H', bars=s.n, train_from=iso(s.ts[start]), train_to=iso(s.ts[cut - 1]),
                  test_from=iso(s.ts[cut]), test_to=iso(s.ts[mid - 1]), final_from=iso(s.ts[mid]), final_to=iso(s.ts[end - 1]),
                  train_share=D.TRAIN_SHARE, test_share=D.TEST_SHARE),
        gate=E.GATE, final_gate=E.FINAL_GATE, volatility=obs, history=state['history'][-500:], timing=timing,
        species_names=SPECIES_NAMES, gene_bounds={k: [v[1], v[2]] if v[0] != 'choice' else v[1] for k, v in GENES.items()},
        nodes=[node_view(n, curves=n['id'] == champ, slim=n['id'] not in seated) for n in nodes.values()],
        hall=state['hall'], champion=node_view(nodes[champ], curves=True) if champ in nodes else None,
        champion_since=iso(state['champion_since']) if state.get('champion_since') else None,
        champion_rules=dict(min_tenure_hours=E.MIN_TENURE // 3600_000, streak=E.STREAK, retain=E.RETAIN, margin=E.SWITCH_MARGIN,
                            team_size=E.TEAM_SIZE, species_cap=E.TEAM_SPECIES_CAP, direction_cap=E.TEAM_DIRECTION_CAP),
        team=[dict(node_view(nodes[m['id']]), since=iso(m['since'])) for m in state.get('team', []) if m['id'] in nodes],
        challengers=[dict(id=nid, species=nodes[nid]['species'], fitness=nodes[nid]['fitness'], test=nodes[nid]['test'], streak=state.get('streaks', {}).get(nid, 0))
                     for nid in state['hall'] if nid not in {m['id'] for m in state.get('team', [])} and nid in nodes][:5],
        paper=paper_view)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--generations', type=int, default=5)
    ap.add_argument('--budget', type=float, default=420, help='seconds; stop breeding early when exceeded')
    ap.add_argument('--reset', action='store_true')
    ap.add_argument('--candles', type=Path, help='candle cache directory (default <data>/evolve/candles)')
    args = ap.parse_args(argv)
    t0, now = time.time(), int(time.time() * 1000)
    out = args.data / 'evolve'; out.mkdir(parents=True, exist_ok=True)
    series = D.load_series(args.data, now, cache_dir=args.candles)
    print(f'{len(series)} 个币 · {next(iter(series.values())).n} 根 1H K 线 · 数据用时 {time.time() - t0:.0f}s')
    split = D.split(series)
    state_path = out / 'state.json'
    if args.reset or not state_path.exists():
        state = E.init_state(seed=now % 1_000_000, series=series, split=split)
    else:
        state = json.loads(state_path.read_text())
        E.refresh(state, series, split)
    migrants_path = Path(__file__).with_name('migrants.json')
    if migrants_path.exists():
        for nid in E.add_migrants(state, json.loads(migrants_path.read_text()), series, split):
            print(f"旧系统迁入 {nid} · {state['nodes'][nid].get('origin')}")
    obs, rows = None, []
    for _ in range(args.generations):
        obs = D.observe_volatility(series)            # observe first, then breed
        rows.append(E.generation(state, series, split, obs))
        if time.time() - t0 > args.budget:
            break
    obs = obs or D.observe_volatility(series)
    changes = E.choose_team(state, now)
    champion, switched = state['champion'], bool(changes)
    paper_view, signals = P.update(state, series, changes)
    E.prune(state)
    timing = dict(seconds=round(time.time() - t0, 1), generations=len(rows), switched=switched, changes=[dict(slot=c[0], old=c[1], new=c[2], reason=c[3]) for c in changes])
    state_path.write_text(json.dumps(state, separators=(',', ':')))
    (out / 'dashboard.json').write_text(json.dumps(dashboard(state, series, split, obs, paper_view, timing), ensure_ascii=False, separators=(',', ':')))
    (out / 'nodes.json').write_text(json.dumps(node_details(state), ensure_ascii=False, separators=(',', ':')))
    (out / 'signals.json').write_text(json.dumps(dict(updated_at=iso(now), champion=champion, team=[m['id'] for m in state.get('team', [])],
                                                  instruments=list(series), positions=signals), indent=1))
    for r in rows:
        print(f"第 {r['gen']} 代 · 波动 {r['regime']} ({r['vol']}) · 最佳适应度 {r['best']} · 平均 {r['mean']} · 合格 {r['qualified']} · 新生 {r['born']} · 淘汰 {r['killed']}")
    for c in changes:
        print(f"席位 {c[0] + 1}: {c[1] or '空'} → {c[2]}（{c[3]}）")
    print(f"冠军组合 {[m['id'] for m in state.get('team', [])]} · 领头 {champion} · 纸面权益 {paper_view['equity']} · 用时 {timing['seconds']}s")


if __name__ == '__main__':
    main()

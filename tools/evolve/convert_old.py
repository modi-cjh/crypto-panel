"""One-off: convert strategies from the old rolling-evolution system (modi-cjh/trading-evo, trading/ga/runs/<v>/state.json)
into this engine's genomes, written to tools/evolve/migrants.json. run.py imports them once (evolution.add_migrants).

Mapping (approximate where the old system had something this engine does not):
  vol_breakout k → vol_breakout expand · ma_cross fast/slow → trend · channel n → momentum lookback
  vol_surge m/n → vol_surge vol_mult/lookback · rel_strength n/th → rel_strength lookback/rs_th
  rsi_revert lo/hi → mean_revert (z-score 20 bars, threshold from how far the RSI band is from 50)
  tf 1h/4h/1d → 1/4/24 (15m strategies are skipped) · exit trail → trail, no target · tp → target · tp_trail → both
  dir, stop_atr, max_hold (bars), risk carry over; the old EMA trend filter has no counterpart and is dropped;
  the volatility filter is left wide open (0.3–4.0).
Usage: python3 convert_old.py <state.json> [--version v2] [--per-species 2]
"""
import argparse
import json
from pathlib import Path

from genome import fix

TF = {'1h': 1, '4h': 4, '1d': 24}
SPECIES = {'vol_breakout': 'vol_breakout', 'ma_cross': 'trend', 'channel': 'momentum', 'vol_surge': 'vol_surge',
           'rel_strength': 'rel_strength', 'rsi_revert': 'mean_revert'}


def convert(g):
    if g.get('tf') not in TF:
        return None
    sp = SPECIES[g['species']]
    exit_ = g.get('exit', 'trail')
    out = dict(species=sp, direction=g.get('dir', 'both'), tf=TF[g['tf']], stop_atr=g['stop_atr'],
               target_rr=0.0 if exit_ == 'trail' else g.get('tp_r', 0.0), trail=0 if exit_ == 'tp' else 1,
               max_hold=g['max_hold'], risk=g.get('risk', .01), vol_min=.3, vol_max=4.0,
               lookback=20, threshold=2.0, expand=2.0, fast=12, slow=48, vol_mult=3.0, rs_th=.1)
    if sp == 'vol_breakout': out['expand'] = g['k']
    if sp == 'trend': out.update(fast=g['fast'], slow=g['slow'])
    if sp == 'momentum': out['lookback'] = g['n']
    if sp == 'vol_surge': out.update(vol_mult=g['m'], lookback=g['n'])
    if sp == 'rel_strength': out.update(lookback=g['n'], rs_th=g['th'])
    if sp == 'mean_revert': out['threshold'] = 1.0 + (50 - g['lo'] + g['hi'] - 50) / 20
    return fix(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('state'); ap.add_argument('--version', default='v2'); ap.add_argument('--per-species', type=int, default=2)
    args = ap.parse_args()
    st = json.loads(Path(args.state).read_text())
    picked, seen = [], set()
    def add(i, why):
        g = convert(i['genes'])
        key = json.dumps(g, sort_keys=True) if g else None
        if g and key not in seen:
            seen.add(key); picked.append(dict(genes=g, origin=f"{args.version} {i['id']} · {why}"))
    act = st.get('active') or {}
    for sid in act.get('ids', []):
        add(dict(id=sid, genes=act['genes'][sid]), '最后的上线组合')
    for sp, members in st['pop'].items():
        for i in sorted(members, key=lambda x: -x.get('fit', -9))[:args.per_species]:
            add(i, '物种前列')
    out = Path(__file__).with_name('migrants.json')
    out.write_text(json.dumps(dict(version=args.version, strategies=picked), ensure_ascii=False, indent=1))
    print(out, len(picked), 'strategies')


if __name__ == '__main__':
    main()

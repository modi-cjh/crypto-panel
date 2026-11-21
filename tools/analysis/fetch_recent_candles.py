#!/usr/bin/env python3
"""Append the newest CLOSED 1H candles (public GET market/candles, no keys) to a candle cache dir in the evolve format
({inst, bar, rows: [[ts, o, h, l, c, quote volume]]}). Used only by tools/analysis/trail_risk_replay.py (recent period).
usage: fetch_recent_candles.py <cache dir> <universe.json>"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evolve'))
import data as D  # noqa: E402


def main(cache, universe):
    cache = Path(cache)
    for inst in json.loads(Path(universe).read_text())['instruments']:
        path = cache / f'{inst}.json'
        body = json.loads(path.read_text()) if path.exists() else dict(inst=inst, bar='1H', rows=[])
        have = {r[0]: r for r in body['rows']}
        for r in D._rows(D.okx_get(f'market/candles?instId={inst}&bar=1H&limit=100')):   # closed bars only (confirm == 1)
            have[r[0]] = r
        body['rows'] = [have[t] for t in sorted(have)]
        path.write_text(json.dumps(body))
        print(inst, len(body['rows']), body['rows'][-1][0])


if __name__ == '__main__':
    main(*sys.argv[1:3])

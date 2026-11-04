"""Data layer: real OKX 1H candles (public endpoints, no keys), cached in the paper-data branch.

- First run back-fills WINDOW_DAYS; later runs fetch only the new bars. Only closed bars (confirm == 1) are kept.
- All instruments share one timeline (timestamps present for every instrument).
- The window is split by time: the first TRAIN_SHARE for training, the rest is out-of-sample.
"""
import json
import subprocess
import time
from pathlib import Path

from execution import Series

INSTRUMENTS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP']
WINDOW_DAYS = 365
TRAIN_SHARE = .7
WARMUP = 250          # bars used only to warm indicators up before the training window
HOUR = 3600_000
_next = 0.0


def okx_get(path, retries=4):
    """GET a public OKX endpoint with curl, paced well under the history-candles limit (20 requests / 2 s)."""
    global _next
    for attempt in range(retries):
        wait = _next - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _next = time.monotonic() + .15
        try:
            out = subprocess.run(['curl', '-sS', '-m', '20', 'https://www.okx.com/api/v5/' + path], capture_output=True, text=True, check=True).stdout
            body = json.loads(out)
            if body.get('code') == '0':
                return body['data']
        except (subprocess.CalledProcessError, ValueError):
            pass
        time.sleep(1 + attempt)
    raise RuntimeError(f'OKX request failed: {path.split("?")[0]}')


def _rows(raw):
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in raw if len(r) < 9 or r[8] == '1']


def update_candles(cache_dir, inst, now_ms, get=okx_get):
    """Load the cached candles for inst, append any new closed bars, trim to the window, save. Returns rows (oldest first)."""
    path = Path(cache_dir) / f'{inst}.json'
    rows = json.loads(path.read_text())['rows'] if path.exists() else []
    have = {r[0] for r in rows}
    since = now_ms - WINDOW_DAYS * 24 * HOUR
    last = rows[-1][0] if rows else since - HOUR
    fresh = _rows(get(f'market/candles?instId={inst}&bar=1H&limit=300'))
    oldest = min((r[0] for r in fresh), default=now_ms)
    # Page backwards through history until the gap to the cache (or the window start) is filled.
    while oldest > last + HOUR and oldest > since:
        page = _rows(get(f'market/history-candles?instId={inst}&bar=1H&after={oldest}&limit=100'))
        if not page:
            break
        fresh += page
        oldest = min(r[0] for r in page)
    rows += [r for r in fresh if r[0] not in have and r[0] >= since]
    rows = sorted({r[0]: r for r in rows if r[0] >= since}.values())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'inst': inst, 'bar': '1H', 'rows': rows}, separators=(',', ':')))
    return rows


def load_series(data_dir, now_ms, get=okx_get, instruments=INSTRUMENTS):
    """Update every instrument and return {inst: Series} on the shared timeline."""
    cache = Path(data_dir) / 'evolve' / 'candles'
    per = {inst: update_candles(cache, inst, now_ms, get) for inst in instruments}
    common = sorted(set.intersection(*(set(r[0] for r in rows) for rows in per.values())))
    keep = set(common)
    out = {}
    for inst, rows in per.items():
        rows = [r for r in rows if r[0] in keep]
        out[inst] = Series(inst, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows])
    return out


def split(series):
    """Index ranges on the shared timeline: (train_start, train_end, test_end). Training starts after the warm-up."""
    n = next(iter(series.values())).n
    start = min(WARMUP, n // 10)
    cut = start + int((n - start) * TRAIN_SHARE)
    return start, cut, n


def observe_volatility(series, bars=24):
    """Each generation first reads the market: ATR% versus its usual level, per coin and overall (median).
    The regime sets how boldly the next generation mutates: calm markets explore less, stormy markets more."""
    per = {}
    for inst, s in series.items():
        vr = s.vol_ratio()[-bars:]
        per[inst] = round(sum(vr) / len(vr), 3)
    vals = sorted(per.values())
    market = vals[len(vals) // 2]
    regime = 'calm' if market < .85 else 'storm' if market > 1.25 else 'normal'
    rate = {'calm': .15, 'normal': .25, 'storm': .35}[regime]
    return dict(market=market, regime=regime, per_inst=per, mutation_rate=rate,
                label={'calm': '平静', 'normal': '正常', 'storm': '剧烈'}[regime])

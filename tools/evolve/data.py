"""Data layer: real OKX 1H candles (public endpoints, no keys), cached between runs (GitHub Actions cache).

- The coins: the UNIVERSE_SIZE most traded USDT perpetuals (crypto only, 24h quote volume) that have traded for the
  whole window, chosen once and kept in <data>/evolve/universe.json so the paper account replays on a fixed set.
  A coin that stops updating (delisted) is dropped from the shared timeline.

- First run back-fills WINDOW_DAYS; later runs fetch only the new bars. Only closed bars (confirm == 1) are kept.
- All instruments share one timeline (timestamps present for every instrument).
- Each row keeps the quote volume (USDT); caches written before volume was stored are back-filled again once.
- Every series except BTC gets BTC as its `ref` (relative-strength species).
- The window is split by time: the first TRAIN_SHARE for training, the rest is out-of-sample.
"""
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from execution import Series

INSTRUMENTS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP']   # fallback / tests
UNIVERSE_SIZE = 100
WORKERS = 4           # parallel downloads; the shared pacing below keeps the total under OKX's rate limit
WINDOW_DAYS = 365
TRAIN_SHARE = .7
WARMUP = 250          # bars used only to warm indicators up before the training window
HOUR = 3600_000
REF = 'BTC-USDT-SWAP'
_next = 0.0
_lock = threading.Lock()


def okx_get(path, retries=4):
    """GET a public OKX endpoint with curl, paced well under the history-candles limit (20 requests / 2 s)."""
    global _next
    for attempt in range(retries):
        with _lock:                       # one request every 0.12 s across all threads (limit: 20 per 2 s)
            slot = max(_next, time.monotonic())
            _next = slot + .12
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)
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
    """[ts, open, high, low, close, quote volume] for closed bars (OKX: r[7] = volCcyQuote, r[8] = confirm)."""
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7]) if len(r) > 7 else 0.0]
            for r in raw if len(r) < 9 or r[8] == '1']


def update_candles(cache_dir, inst, now_ms, get=okx_get):
    """Load the cached candles for inst, append any new closed bars, trim to the window, save. Returns rows (oldest first)."""
    path = Path(cache_dir) / f'{inst}.json'
    rows = json.loads(path.read_text())['rows'] if path.exists() else []
    if rows and len(rows[0]) < 6:      # cache from before volume was stored: fetch the window again
        rows = []
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


def select_universe(now_ms, get=okx_get, size=UNIVERSE_SIZE):
    """The `size` most traded crypto USDT perpetuals by 24h quote volume, listed for the whole window. BTC always included."""
    live = {d['instId']: d for d in get('public/instruments?instType=SWAP')
            if d.get('settleCcy') == 'USDT' and d.get('state') == 'live' and str(d.get('instCategory', '1')) == '1'}
    oldest = now_ms - (WINDOW_DAYS + 14) * 24 * HOUR
    ok = {k for k, d in live.items() if d.get('listTime') and int(d['listTime']) <= oldest}
    vol = {t['instId']: float(t.get('volCcy24h') or 0) * float(t.get('last') or 0) for t in get('market/tickers?instType=SWAP') if t['instId'] in ok}
    ranked = sorted(vol, key=vol.get, reverse=True)
    picked = [REF] + [k for k in ranked if k != REF][:size - 1]
    return picked


def universe(data_dir, now_ms, get=okx_get):
    """The fixed coin list in <data>/evolve/universe.json; chosen on first use."""
    path = Path(data_dir) / 'evolve' / 'universe.json'
    if path.exists():
        return json.loads(path.read_text())['instruments']
    picked = select_universe(now_ms, get)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(chosen_at=now_ms, rule=f'top {UNIVERSE_SIZE} USDT perpetuals by 24h volume, listed ≥ {WINDOW_DAYS} days',
                                    instruments=picked), indent=1))
    return picked


def load_series(data_dir, now_ms, get=okx_get, instruments=None, cache_dir=None):
    """Update every instrument and return {inst: Series} on the shared timeline. Coins whose candles stopped
    (delisted, suspended) more than 3 hours before the freshest coin are left out."""
    instruments = instruments or universe(data_dir, now_ms, get)
    cache = Path(cache_dir) if cache_dir else Path(data_dir) / 'evolve' / 'candles'
    with ThreadPoolExecutor(WORKERS) as pool:
        got = dict(zip(instruments, pool.map(lambda inst: _safe_update(cache, inst, now_ms, get), instruments)))
    per = {k: v for k, v in got.items() if v}
    newest = max(rows[-1][0] for rows in per.values())
    per = {k: v for k, v in per.items() if v[-1][0] >= newest - 3 * HOUR}
    common = sorted(set.intersection(*(set(r[0] for r in rows) for rows in per.values())))
    keep = set(common)
    out = {}
    for inst, rows in per.items():
        rows = [r for r in rows if r[0] in keep]
        out[inst] = Series(inst, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows],
                           [r[5] for r in rows])
    ref = out.get(REF)
    for inst, s in out.items():
        if inst != REF:
            s.ref = ref
    return out


def _safe_update(cache, inst, now_ms, get):
    try:
        return update_candles(cache, inst, now_ms, get)
    except RuntimeError as e:             # one coin failing must not stop the run
        print(f'跳过 {inst}: {e}')
        return []


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

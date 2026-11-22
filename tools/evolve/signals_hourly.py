#!/usr/bin/env python3
"""Hourly signals only (live server, step 1): replay the published paper account on the newest closed 1H bar.

Runs at H:01:30 UTC on the live server (deploy/live/okx-signals.timer). It does NOT evolve: no E.refresh /
E.generation / E.choose_team / D.price_check, nothing is stamped, nothing is published, no paper-data write, no
credentials (public GitHub raw files and public OKX market data only). Evolution, seat changes and the paper-data
publication stay in GitHub Actions every 4 h; their changes take effect on the next whole-hour bar (paper.py
NEXT-HOUR RULE), so this replay of a stale or a fresh state.json gives the same signals (see the PR1 contract).

One run:
  1. lock <data>/.signals.lock (one run at a time; the shadow and the live unit share the candle cache);
  2. source: evolve/state.json, universe.json, dashboard.json of the published paper-data (resolved to one commit via
     the public GitHub API when possible, so the three files belong together; raw CDN caches a branch URL ~5 min).
     generation / last published bar must not go back: an older copy is ignored and the local previous one is used;
  3. candles: incremental public OKX 1H candles into the cache. COMPLETENESS: every coin's last CONFIRMED bar must be
     target = floor(now, 1 h) − 1 h (a coin more than data.STALE_HOURS behind is left out exactly as load_series does).
     Incomplete coins are fetched again every --retry seconds until --deadline (H:05 by default). At the deadline
     requests in flight are cut too (curl killed, silent okx_get retries and pacing waits ended): still incomplete →
     exit 2, loud log, signals.json not written (the trader keeps last hour's file). load_series would silently drop
     the whole timeline back one bar instead (intersection), which is what this check prevents;
  4. funding: a coin whose own funding interval has a settlement on the target bar must have that record; missing
     records are fetched again until --funding-deadline (H:08, requests in flight cut as well); still missing → a
     warning in the output (an interval change on OKX's side must not stop trading; the effect is one funding payment
     on one bar), but a coin left with no rates at all → exit 2;
  5. replay: paper.update(state, series, [], price_off, replay=True), which never changes the state (raises paper.StateDrift when the state
     predates the next-hour rule or its seats disagree with its team → exit 3, nothing written);
  6. write <data>/evolve/signals.json atomically (tmp + rename; --shadow: signals.shadow.json) with the usual fields
     (updated_at, champion, team, instruments, positions, exits — what okx_demo_trader reads) plus source / version /
     last_bar / state generation + paper-data commit / code commit / timing; append the same line to an hourly
     archive (<data>/archive or, --shadow, <data>/shadow, one jsonl per UTC day) and write <data>/last_run.json.

Deadlines bind the data phases; the replay and the write follow them (a few seconds). Last resort for anything else:
a watchdog exits 2 HARD_GRACE_S after the funding deadline (H:09:30), well before systemd's TimeoutStartSec. A run started
after --deadline gets --late-budget seconds. Candle cache files may be updated by a failed run; signals files never.

Exit codes: 0 written · 2 candles incomplete at the deadline · 3 source / state unusable · 4 another run holds the
lock · 1 anything else. Every failure is also appended to the archive (ok: false) and last_run.json.
"""
import argparse
import copy
import fcntl
import gc
import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data as D        # noqa: E402
import paper as P       # noqa: E402

VERSION = 1                                 # signals.json 'version' of this producer
SOURCE_NAME = 'server-hourly'               # signals.json 'source' (GitHub's run.py writes no 'source')
HOUR = 3600_000
REPO = 'modi-cjh/crypto-panel'
DEFAULT_SOURCE = f'https://raw.githubusercontent.com/{REPO}/paper-data'
DEFAULT_COMMIT_API = f'https://api.github.com/repos/{REPO}/commits/paper-data'
DEFAULT_MASTER_API = f'https://api.github.com/repos/{REPO}/commits/master'
DEFAULT_DATA = Path('/var/lib/modi-okx-signals')
SOURCE_FILES = ('state.json', 'universe.json', 'dashboard.json')
ARCHIVE_DAYS = 14
EXIT_INCOMPLETE, EXIT_SOURCE, EXIT_LOCKED, EXIT_ERROR = 2, 3, 4, 1
SOURCE_BUDGET_S = 60        # all GitHub requests of one run together; then the local copy is used
MASTER_BUDGET_S = 5         # the optional "is the code = master" check
LAST_TRY_S = 3              # the last retry round starts this long before the deadline
HARD_GRACE_S = 90           # local work (replay, write) after the last deadline; then the watchdog exits 2


class Fail(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def log(msg):
    print(msg, flush=True)


def atomic_write(path, text):
    """tmp file in the same directory + fsync + rename: readers see the old or the new file, never a partial one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)              # public market-derived data; the trader (another user) reads it
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def curl_text(url, timeout=30):
    """GET a public URL (no credentials, no auth header). Raises RuntimeError; never outlasts `timeout` seconds."""
    try:
        out = subprocess.run(['curl', '-sS', '-f', '-m', f'{timeout:.3f}', '-H', 'Cache-Control: no-cache', '-H', 'User-Agent: modi-signals', url],
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'GET {url.split("?")[0]} timed out') from None
    if out.returncode != 0:
        raise RuntimeError(f'GET {url.split("?")[0]} failed (curl {out.returncode})')
    return out.stdout


# ---- deadlines ----------------------------------------------------------------------------------------------------
def minute_of_hour(now_ms, minute):
    return now_ms // HOUR * HOUR + int(minute * 60_000)


def deadlines(args, start_ms):
    """(candle deadline, funding deadline) in ms. On time (the timer, H:01:30): H:05 and H:08. Started after the candle
    deadline (a manual run): --late-budget seconds from the start, for both, and never into the next hour (its own run
    has a newer bar). Always enforced: see fetch_round / okx_get(deadline=) / Watchdog."""
    end = start_ms // HOUR * HOUR + HOUR
    candles = minute_of_hour(start_ms, args.deadline)
    if start_ms >= candles or args.as_of is not None:
        candles = start_ms + int(args.late_budget * 1000)
    candles = min(candles, end)
    return candles, min(max(minute_of_hour(start_ms, args.funding_deadline), candles), end)


def bound_get(get, deadline_ms, now_fn):
    """The getter for one phase. None = OKX itself, bound to the phase's deadline (wall clock → monotonic)."""
    if get is not None:
        return get
    end = time.monotonic() + (deadline_ms - now_fn()) / 1000
    return lambda path: D.okx_get(path, deadline=end)


def bound_http(http, deadline_ms, now_fn):
    """http(url, timeout) whose timeouts are cut to what is left until deadline_ms; nothing starts after it."""
    def get(url, timeout=30):
        left = (deadline_ms - now_fn()) / 1000
        if left <= 0:
            raise RuntimeError(f'GET {url.split("?")[0]}: no time left')
        return http(url, min(timeout, left))
    return get


def fetch_round(fn, items, workers, deadline_ms, now_fn):
    """[fn(item)] in parallel, at most until deadline_ms: items not finished by then come back as (item, None, reason)
    and their threads are abandoned (the process ends with os._exit; the cache files they may still write are atomic)."""
    pool = ThreadPoolExecutor(workers)
    try:
        futs = [pool.submit(fn, i) for i in items]
        done, _ = wait_futures(futs, timeout=max(0.0, (deadline_ms - now_fn()) / 1000))
        return [f.result() if f in done else (i, None, 'aborted at the deadline') for i, f in zip(items, futs)]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


class Watchdog:
    """Last resort for anything the deadlines above cannot cut (a stuck disk, a runaway replay): exits the process with
    code 2 at limit_s. disarm() right before the output is written: False = it already fired, do not write."""

    def __init__(self, limit_s, on_fire, exit_fn=os._exit):
        self.lock, self.fired, self.off, self.on_fire, self.exit_fn = threading.Lock(), False, False, on_fire, exit_fn
        self.timer = threading.Timer(limit_s, self._fire)
        self.timer.daemon = True
        self.timer.start()

    def _fire(self):
        with self.lock:
            if self.off:
                return
            self.fired = True
        t = threading.Thread(target=self.on_fire, daemon=True)      # may block on the same stuck disk / pipe
        t.start()
        t.join(3)
        self.exit_fn(EXIT_INCOMPLETE)

    def disarm(self):
        with self.lock:
            self.off = not self.fired
        if self.off:
            self.timer.cancel()
        else:
            self.timer.join(5)                                      # it records the failure, then ends the process
        return self.off


# ---- source: the published state ----------------------------------------------------------------------------------
def _meta(state, dashboard):
    return dict(generation=state.get('generation'), prev_last=_prev_last(dashboard),
                next_hour_from=((state.get('paper') or {}).get('account') or {}).get('next_hour_from'),
                published_at=dashboard.get('updated_at'))


def _prev_last(dashboard):
    try:
        return int(dashboard['paper']['curve'][-1][0])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _older(new, old):
    """True when the fetched copy is older than the one kept locally (generation, then the last published bar)."""
    g1, g0 = new.get('generation'), old.get('generation')
    if isinstance(g1, int) and isinstance(g0, int) and g1 != g0:
        return g1 < g0
    p1, p0 = new.get('prev_last'), old.get('prev_last')
    return isinstance(p1, int) and isinstance(p0, int) and p1 < p0


def _resolve_commit(api, http):
    if not api:
        return None
    try:
        sha = json.loads(http(api, 20)).get('sha')
        return sha if isinstance(sha, str) and len(sha) == 40 and all(c in '0123456789abcdef' for c in sha) else None
    except (RuntimeError, ValueError, AttributeError) as e:
        log(f'警告：解析 paper-data 提交号失败（{e}），改用分支地址')
        return None


def load_source(src, commit_api, keep_dir, http=curl_text):
    """(state, universe, dashboard, info). src = a raw base URL ending in the ref (…/paper-data) or a local directory
    holding evolve/<files>. The copy actually used is kept in keep_dir (with meta.json) for the monotonic check."""
    keep_dir = Path(keep_dir)
    kept_meta = json.loads((keep_dir / 'meta.json').read_text()) if (keep_dir / 'meta.json').exists() else None

    def kept():
        if not kept_meta:
            return None
        try:
            docs = [json.loads((keep_dir / f).read_text()) for f in SOURCE_FILES]
        except (OSError, ValueError):
            return None
        return (*docs, dict(kept_meta, fetched=False))

    info = dict(src=str(src), commit=None, fetched=True)
    try:
        if str(src).startswith(('http://', 'https://')):
            sha = _resolve_commit(commit_api, http)
            if sha and kept_meta and kept_meta.get('commit') == sha:      # same commit as last hour: no download needed
                k = kept()
                if k:
                    log(f'来源：paper-data {sha[:7]}（与上次相同，用本地副本）')
                    return k[:3] + (dict(k[3], fetched=True, unchanged=True),)
            base = str(src).rstrip('/')
            if sha:
                base = base.rsplit('/', 1)[0] + '/' + sha
            info['commit'] = sha
            docs = [json.loads(http(f'{base}/evolve/{f}', 60)) for f in SOURCE_FILES]
        else:
            root = Path(src)
            docs = [json.loads((root / 'evolve' / f).read_text()) for f in SOURCE_FILES]
            head = root / '.commit'                                   # optional: a local copy may note its commit here
            info['commit'] = head.read_text().strip() if head.exists() else None
        state, universe, dashboard = docs
        if not isinstance(state, dict) or not isinstance(universe, dict) or not isinstance(dashboard, dict) \
                or 'paper' not in state or 'instruments' not in universe:
            raise ValueError('source files have an unexpected shape')
        if dashboard.get('generation') is not None and dashboard.get('generation') != state.get('generation'):
            raise ValueError(f"state.json generation {state.get('generation')} ≠ dashboard.json {dashboard.get('generation')} (CDN mix)")
    except (RuntimeError, ValueError, OSError) as e:
        k = kept()
        if not k:
            raise Fail(EXIT_SOURCE, f'读取已发布的 state.json 失败且本地没有副本：{e}')
        log(f'警告：读取已发布的 state.json 失败（{e}），沿用本地上一份 generation {k[3].get("generation")}')
        return k
    meta = dict(info, **_meta(state, dashboard))
    if kept_meta and _older(meta, kept_meta):
        k = kept()
        if k:
            log(f"警告：读到的 state 比本地旧（generation {meta['generation']} < {kept_meta.get('generation')} 或最后 K 线回退，raw CDN 缓存），沿用本地上一份")
            return k[:3] + (dict(k[3], regressed_from=meta),)
    for name, doc in zip(SOURCE_FILES, (state, universe, dashboard)):
        atomic_write(keep_dir / name, json.dumps(doc, separators=(',', ':')))
    atomic_write(keep_dir / 'meta.json', json.dumps(meta, indent=1))
    return state, universe, dashboard, meta


# ---- candles: completeness ------------------------------------------------------------------------------------------
def fetch_complete(instruments, cache, now_fn, target, deadline_ms, retry_s, sleep=time.sleep, get=None, workers=D.WORKERS,
                   t0=None, cut=False):
    """{inst: rows} with every coin's last confirmed bar == target, or Fail(EXIT_INCOMPLETE) at the deadline, which also
    cuts requests in flight. get=None: OKX.
    Coins more than data.STALE_HOURS behind target (delisted / suspended) are returned too; assemble() leaves them out
    exactly like load_series. A coin whose fetch fails, or that lags 1..STALE_HOURS bars, is incomplete."""
    got, todo, attempt = {}, list(instruments), 0
    t0 = now_fn() if t0 is None else t0      # one clock for the window trim of every coin (as load_series)
    get = bound_get(get, deadline_ms, now_fn)

    def one(inst):
        try:
            rows = D.update_candles(cache, inst, t0, get)
            return inst, [r for r in rows if r[0] <= target] if cut else rows, None      # --as-of: bars up to target only
        except (RuntimeError, OSError, ValueError) as e:
            return inst, None, str(e)
    while True:
        attempt += 1
        res = fetch_round(one, todo, workers, deadline_ms, now_fn)
        bad = {}
        for inst, rows, err in res:
            if rows is None:
                bad[inst] = f'fetch failed: {err}'
                continue
            got[inst] = rows
            last = rows[-1][0] if rows else None
            if last == target:
                continue
            if last is not None and last > target:
                bad[inst] = f'last bar {iso(last)} after target {iso(target)} (clock?)'
            elif last is not None and last < target - D.STALE_HOURS * HOUR:
                continue                                        # stopped coin: left out by assemble(), like load_series
            else:
                bad[inst] = f'last confirmed bar {iso(last) if last else "none"} ≠ {iso(target)}'
        now = now_fn()
        if not bad and now <= deadline_ms:
            return got, attempt
        pause = min(retry_s, (deadline_ms - now) / 1000 - LAST_TRY_S)
        if pause <= 0:
            detail = '; '.join(f'{k}: {v}' for k, v in sorted(bad.items())[:10]) or '全部到齐时已过截止'
            raise Fail(EXIT_INCOMPLETE, f'K 线不齐：{len(bad)} 个币到截止 {iso(deadline_ms)} 仍缺 {iso(target)} 这根已收盘 K 线（第 {attempt} 次）：{detail}')
        log(f'K 线不齐（第 {attempt} 次）：{len(bad)} 个币还没有 {iso(target)}（{", ".join(sorted(bad)[:6])}…），{pause:.0f}s 后重试')
        sleep(pause)
        todo = sorted(bad)


def funding_missing(rows, target):
    """True when this coin's own settlement interval (gap of its last two records) puts a settlement on the target bar
    and that record is not there yet. Coins without enough history are not checked (funding_map uses the default)."""
    if not rows or len(rows) < 2:
        return False
    last, step = rows[-1][0], rows[-1][0] - rows[-2][0]
    if step <= 0 or last >= target:
        return False
    return (target - last) % step == 0


def fetch_funding(instruments, cache, now_fn, target, deadline_ms, retry_s, sleep=time.sleep, get=None, workers=D.WORKERS,
                  t0=None, cut=False):
    """{inst: funding rows}, waiting for a settlement on the target bar until the funding deadline, which also cuts
    requests in flight. Then the cached rates are used, with a warning (a late record must not stop trading); a coin with
    no rates at all because its request failed is Fail(EXIT_INCOMPLETE): its whole history would fall back to the default
    rate. Returns (rows, warnings)."""
    out, todo, warnings = {}, list(instruments), []
    get = bound_get(get, deadline_ms, now_fn)

    def one(inst):
        try:
            rows = D.update_funding(cache, inst, now_fn() if t0 is None else t0, get)
            return inst, [r for r in rows if r[0] <= t0] if cut else rows, None
        except (RuntimeError, OSError, ValueError) as e:
            return inst, None, str(e)
    while True:
        res = fetch_round(one, todo, workers, deadline_ms, now_fn)
        bad, empty = [], []
        for inst, rows, err in res:
            if rows is None:
                path = Path(cache) / 'funding' / f'{inst}.json'          # keep the cached rates; a fetch error is retried
                rows = json.loads(path.read_text())['rows'] if path.exists() else []
                rows = [r for r in rows if r[0] <= t0] if cut else rows
                bad.append(inst)
                if not rows:
                    empty.append(inst)
            elif funding_missing(rows, target):
                bad.append(inst)
            out[inst] = rows
        if not bad:
            return out, warnings
        pause = min(retry_s, (deadline_ms - now_fn()) / 1000 - LAST_TRY_S)
        if pause <= 0:
            if empty:
                raise Fail(EXIT_INCOMPLETE, f'资金费：{len(empty)} 个币到截止 {iso(deadline_ms)} 没有任何记录（{", ".join(sorted(empty)[:6])}）')
            warnings.append(f'funding record for {iso(target)} missing (or its request failed) at the funding deadline: {", ".join(sorted(bad))}')
            log('警告：' + warnings[-1])
            return out, warnings
        log(f'资金费记录还没出现：{len(bad)} 个币（{", ".join(sorted(bad)[:6])}），{pause:.0f}s 后重试')
        sleep(pause)
        todo = sorted(bad)


# ---- code version -------------------------------------------------------------------------------------------------
def code_commit(root=Path(__file__).resolve().parents[2]):
    """HEAD of the checkout this script runs from, read from .git without running git (the service user does not own
    the checkout, which git refuses as 'dubious ownership'). None when not a git checkout."""
    try:
        g = Path(root) / '.git'
        if g.is_file():                                             # worktree: "gitdir: <path>"
            g = Path(g.read_text().split(':', 1)[1].strip())
        head = (g / 'HEAD').read_text().strip()
        if not head.startswith('ref:'):
            return head
        ref = head.split(':', 1)[1].strip()
        common = Path((g / 'commondir').read_text().strip()) if (g / 'commondir').exists() else None
        for base in (g, (g / common) if common else None):
            if base and (base / ref).exists():
                return (base / ref).read_text().strip()
            if base and (base / 'packed-refs').exists():
                for line in (base / 'packed-refs').read_text().splitlines():
                    if line.endswith(' ' + ref):
                        return line.split()[0]
    except (OSError, IndexError):
        pass
    return None


# ---- one run ------------------------------------------------------------------------------------------------------
def _no_network(path):
    raise RuntimeError(f'signals_hourly reads universe.json only, not {path.split("?")[0]}')


def produce(args, now_fn=lambda: int(time.time() * 1000), sleep=time.sleep, get=None, http=curl_text):
    """Everything except the lock and the error handling: returns the signals document (not written yet)."""
    t_real = now_fn()                                     # wall clock of this run (t0 differs only with --as-of)
    t0 = args.as_of if args.as_of is not None else t_real
    cut = args.as_of is not None
    hour = t0 // HOUR * HOUR
    target = hour - HOUR                                  # the newest bar that has closed: open time floor(now) − 1 h
    deadline, fdeadline = deadlines(args, t_real)
    data = Path(args.data)
    state, universe, dashboard, src = load_source(args.source, args.commit_api, data / 'source',
                                                   http=bound_http(http, min(t_real + SOURCE_BUDGET_S * 1000, deadline), now_fn))
    del dashboard                                         # only its last published bar is needed (in src['prev_last'])
    log(f"来源：generation {src.get('generation')} · paper-data {str(src.get('commit') or '?')[:7]} · 上次发布最后 K 线 "
        f"{iso(src['prev_last']) if src.get('prev_last') else '?'}{'' if src.get('fetched', True) else '（本地副本）'}")
    master = None                                         # asked here, not after the replay: nothing may delay the write
    if args.master_api and str(args.source).startswith(('http://', 'https://')):
        try:
            master = json.loads(bound_http(http, deadline, now_fn)(args.master_api, MASTER_BUDGET_S)).get('sha')
        except (RuntimeError, ValueError, AttributeError):
            master = None
    work = data / 'work'                                  # universe.json copy for data.universe(); never published
    atomic_write(work / 'evolve' / 'universe.json', json.dumps(universe, indent=1))
    instruments, specs = D.universe(work, t0, _no_network, with_specs=True)
    cache = Path(args.candles)
    t_fetch = now_fn()
    rows, attempts = fetch_complete(instruments, cache, now_fn, target, deadline, args.retry, sleep=sleep, get=get, t0=t0, cut=cut)
    # every coin is checked on its own settlement interval (8 h, 4 h or 1 h on OKX), so no fixed list of funding hours
    funding, warnings = fetch_funding(instruments, cache, now_fn, target, fdeadline, args.retry, sleep=sleep, get=get,
                                      t0=t0 if cut else None, cut=cut)
    series = D.assemble(rows, specs, funding, contracts=True)
    del rows, funding                                     # the raw rows are copied into the series: free them before the replay
    gc.collect()
    t_data = now_fn()
    last = next(iter(series.values())).ts[-1]
    if last != target:                                    # cannot happen after fetch_complete; never write a shifted timeline
        raise Fail(EXIT_INCOMPLETE, f'共享时间轴最后一根 {iso(last)} ≠ {iso(target)}')
    dropped = [k for k in instruments if k not in series]
    if dropped:
        warnings.append(f'coins left out (stopped > {D.STALE_HOURS} h, as load_series): {", ".join(dropped)}')
        log('警告：' + warnings[-1])
    price_off = universe.get('price_off', {})
    legacy = False
    try:                                                  # replay=True never changes the state (PR1 contract; tested)
        view, signals = P.update(state, series, [], price_off, prev_last=src.get('prev_last'), replay=True)
    except P.StateDrift as e:
        if not args.legacy_state:
            raise Fail(EXIT_SOURCE, f'state.json 不能只重放（{e}）；需要 GitHub 运行先盖章，本小时不写信号')
        # Before the first GitHub run with the NEXT-HOUR RULE: replay the way run.py did (old stamps) on a copy.
        # Shadow / analysis only; the trader must never follow this (signals carry legacy_state: true).
        log(f'警告：{e} → --legacy-state：按旧规则在副本上重放（只用于影子 / 对账）')
        legacy = True
        view, signals = P.update(copy.deepcopy(state), series, [], price_off, prev_last=src.get('prev_last'))
    exits = view.pop('exits', {})
    t_end = now_fn()
    code = code_commit()
    if master and code and master != code:
        warnings.append(f'code {code[:7]} ≠ GitHub master {master[:7]}: the replay may differ from the next GitHub run')
        log('警告：' + warnings[-1])
    return dict(
        # the fields run.py writes and okx_demo_trader / fetch_signals read
        updated_at=iso(t0), champion=state.get('champion'), team=[m['id'] for m in state.get('team', [])],
        instruments=list(series), positions=signals, exits=exits,
        # provenance for the trader's freshness check and for compare_signals.py
        source=SOURCE_NAME, version=VERSION, shadow=bool(args.shadow), legacy_state=legacy,
        **({'as_of': iso(t0)} if cut else {}),
        last_bar=iso(last), last_bar_ts=last, computed_at=iso(t_end), equity=view.get('equity'),
        pending=view.get('pending'), scheduled=view.get('scheduled'), dropped=dropped, warnings=warnings,
        state=dict(generation=src.get('generation'), commit=src.get('commit'), published_at=src.get('published_at'),
                   prev_last=src.get('prev_last'), next_hour_from=src.get('next_hour_from'), fetched=src.get('fetched', True),
                   **({'regressed_from': src['regressed_from']} if src.get('regressed_from') else {})),
        code=dict(commit=code, master=master),
        timing=dict(started=iso(t0), data_s=round((t_data - t_fetch) / 1000, 2), replay_s=round((t_end - t_data) / 1000, 2),
                    total_s=round((t_end - t_real) / 1000, 2), attempts=attempts,
                    latency_s=None if cut else round((t_end - (target + HOUR)) / 1000, 1),       # bar close → file written
                    max_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)))


def paths(args):
    data = Path(args.data)
    if args.shadow:
        return data / 'evolve' / 'signals.shadow.json', data / 'shadow'
    return data / 'evolve' / 'signals.json', data / 'archive'


def archive(adir, doc, now_ms):
    adir.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime('%Y-%m-%d')
    line = (json.dumps(doc, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
    fd = os.open(adir / f'signals-{day}.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    cutoff = now_ms - ARCHIVE_DAYS * 24 * HOUR
    for f in adir.glob('signals-*.jsonl'):
        try:
            if datetime.strptime(f.stem[8:], '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp() * 1000 < cutoff:
                f.unlink()
        except ValueError:
            pass


def run(args, now_fn=lambda: int(time.time() * 1000), sleep=time.sleep, get=None, http=curl_text, watchdog=False):
    """watchdog: only for the command line (it ends the process)."""
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    out, adir = paths(args)
    t0 = now_fn()
    lock = open(data / '.signals.lock', 'w')

    def record(code, msg):
        log(f'失败（退出码 {code}）：{msg}；未写 {out.name}，交易程序沿用上一份信号')
        fail = dict(ok=False, code=code, error=msg, at=iso(now_fn()), started=iso(t0), shadow=bool(args.shadow),
                    target=iso(t0 // HOUR * HOUR - HOUR))
        try:
            archive(adir, fail, t0)
            atomic_write(data / 'last_run.json', json.dumps(fail, indent=1, ensure_ascii=False))
        except OSError:
            pass
    dog = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log('另一个 signals_hourly 正在运行，本次退出')
            return EXIT_LOCKED
        if watchdog:
            hard = deadlines(args, t0)[1] + HARD_GRACE_S * 1000
            dog = Watchdog((hard - now_fn()) / 1000, lambda: record(EXIT_INCOMPLETE, f'到硬截止 {iso(hard)} 仍未完成，看门狗中止'))
        try:
            doc = produce(args, now_fn=now_fn, sleep=sleep, get=get, http=http)
        except Fail as e:
            code, msg = e.code, str(e)
        except P.StateDrift as e:
            code, msg = EXIT_SOURCE, f'StateDrift: {e}'
        except Exception as e:                       # noqa: BLE001 — any failure: loud, non-zero, no signals file
            code, msg = EXIT_ERROR, f'{type(e).__name__}: {e}'
            traceback.print_exc()
        else:
            if dog and not dog.disarm():
                return EXIT_INCOMPLETE                   # the watchdog is ending the process: write nothing
            atomic_write(out, json.dumps(doc, indent=1, ensure_ascii=False))
            archive(adir, dict(ok=True, **doc), t0)
            atomic_write(data / 'last_run.json', json.dumps(dict(ok=True, at=iso(now_fn()), shadow=bool(args.shadow), file=str(out),
                                                                 last_bar=doc['last_bar'], timing=doc['timing'], warnings=doc['warnings']), indent=1))
            log(f"信号已写入 {out} · 最后 K 线 {doc['last_bar']} · {len(doc['positions'])} 个信号 · 权益 {doc['equity']} · "
                f"数据 {doc['timing']['data_s']}s 重放 {doc['timing']['replay_s']}s · 收盘后 {doc['timing']['latency_s'] if doc['timing']['latency_s'] is not None else '—'}s"
                + (' · 影子模式' if args.shadow else ''))
            return 0
        if dog and not dog.disarm():
            return EXIT_INCOMPLETE
        record(code, msg)
        return code
    finally:
        lock.close()


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data', type=Path, default=DEFAULT_DATA, help='output / state directory (default %(default)s)')
    ap.add_argument('--candles', type=Path, help='candle cache (default <data>/candles)')
    ap.add_argument('--source', default=DEFAULT_SOURCE, help='published paper-data: raw base URL ending in the ref, or a local directory with evolve/')
    ap.add_argument('--commit-api', default=DEFAULT_COMMIT_API, help="public GitHub API URL resolving the ref to a commit ('' = use the branch URL)")
    ap.add_argument('--master-api', default=DEFAULT_MASTER_API, help="public GitHub API URL of master, to warn when the code differs ('' = off)")
    ap.add_argument('--deadline', type=float, default=5, help='minute of the hour after which incomplete candles fail the run (default %(default)s → H:05)')
    ap.add_argument('--funding-deadline', type=float, default=8, help='minute of the hour to wait until for a funding record on the target bar (default %(default)s)')
    ap.add_argument('--late-budget', type=float, default=210, help='seconds a run started after --deadline (or with --as-of) may take to get its data '
                                                                    '(default %(default)s, never past the end of the hour)')
    ap.add_argument('--retry', type=float, default=15, help='seconds between completeness retries (default %(default)s)')
    ap.add_argument('--shadow', action='store_true', help='write evolve/signals.shadow.json and <data>/shadow/*.jsonl; the trader never reads them')
    ap.add_argument('--as-of', help='shadow / analysis only: compute as if the clock were this UTC time (ISO), using bars up to '
                                    'floor(as_of) − 1 h from the cache + OKX (to line up with an older GitHub run)')
    ap.add_argument('--legacy-state', action='store_true',
                    help='shadow / analysis only: replay a state that predates the NEXT-HOUR RULE the old way (signals carry legacy_state: true)')
    return ap


def main(argv=None, **kw):
    args = parser().parse_args(argv)
    if args.candles is None:
        args.candles = args.data / 'candles'
    if (args.legacy_state or args.as_of) and not args.shadow:
        log('--legacy-state / --as-of 只能与 --shadow 一起用（交易程序不能跟随这样算出的信号）')
        return EXIT_SOURCE
    if args.as_of:
        args.as_of = int(datetime.fromisoformat(args.as_of.replace('Z', '+00:00')).astimezone(timezone.utc).timestamp() * 1000)
    return run(args, **kw)


if __name__ == '__main__':
    rc = main(watchdog=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)                       # not sys.exit: that would wait for threads abandoned at a deadline

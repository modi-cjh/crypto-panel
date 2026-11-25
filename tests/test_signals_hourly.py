"""tools/evolve/signals_hourly.py (hourly server signals, replay only), the trader's signal timing switch,
fetch_signals.py hourly mode, the systemd units and tools/analysis/compare_signals.py."""
import builtins
import fcntl
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'evolve'))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tools' / 'analysis'))
import data as D               # noqa: E402
import execution as X          # noqa: E402
import paper as P              # noqa: E402
import run as R                # noqa: E402
import signals_hourly as S     # noqa: E402
import okx_demo_trader as odt  # noqa: E402
import fetch_signals as FS     # noqa: E402
import compare_signals as CS   # noqa: E402
from genome import fix as fix_genes, random_genome  # noqa: E402

H = 3600_000
OKX_GET = D.okx_get                            # taken at import: test_evolve's RunTests leaves its fake in data.okx_get
HOUR0 = 1_795_300_000_000 // H * H            # a whole UTC hour in Nov 2026
COINS = ['BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP', 'DOGE-USDT-SWAP']
DAYS = 40                                       # history the fake exchange has (listing date)


def price(inst, ts):
    """Deterministic price path: the same bar always has the same OHLC, whenever it is fetched."""
    k = (ts - (HOUR0 - DAYS * 24 * H)) // H
    x = 100.0 * (1 + COINS.index(inst) if inst in COINS else 1)
    rng = random.Random(f'{inst}')
    drift = rng.gauss(0, .0005)
    return x * math.exp(drift * k + .02 * math.sin(k / 9 + len(inst)) + random.Random(f'{inst}{ts}').gauss(0, .004))


class FakeOKX:
    """Public OKX endpoints as data.okx_get answers them (newest first, r[8] = confirm). The clock is shared with the test.
    lag[inst] = bars the coin's newest confirmed bar is behind; unconfirmed = coins whose newest closed bar is still confirm 0;
    heal_after[inst] = calls after which the lag disappears; fail = coins whose requests raise."""

    def __init__(self, clock, lag=None, unconfirmed=(), heal_after=None, fail=(), funding_lag=None):
        self.clock, self.lag, self.unconfirmed, self.heal = clock, dict(lag or {}), set(unconfirmed), dict(heal_after or {})
        self.fail, self.funding_lag, self.calls = set(fail), dict(funding_lag or {}), []

    def bar_row(self, inst, ts, confirm='1'):
        o, c = price(inst, ts), price(inst, ts + H)
        return [str(ts), repr(o), repr(max(o, c) * 1.003), repr(min(o, c) * .997), repr(c), '0', '0', repr(1e6 + ts % 1000), confirm]

    def newest(self, inst):
        n = sum(1 for p in self.calls if f'instId={inst}&' in p and p.startswith('market/candles'))
        lag = self.lag.get(inst, 0)
        if inst in self.heal and n > self.heal[inst]:
            lag = 0
        return self.clock['now'] // H * H - H - lag * H

    def __call__(self, path):
        self.calls.append(path)
        inst = path.split('instId=')[1].split('&')[0] if 'instId=' in path else None
        if inst in self.fail:
            raise RuntimeError('OKX request failed: fake')
        start = HOUR0 - DAYS * 24 * H
        if path.startswith('market/candles'):
            top = self.newest(inst)
            unconf = inst in self.unconfirmed
            rows = [self.bar_row(inst, top + H, '0')]                      # the bar still open
            for k in range(299):
                ts = top - k * H
                if ts < start:
                    break
                rows.append(self.bar_row(inst, ts, '0' if unconf and k == 0 else '1'))
            return rows
        if path.startswith('market/history-candles'):
            after = int(path.split('after=')[1].split('&')[0])
            return [self.bar_row(inst, after - (k + 1) * H) for k in range(100) if after - (k + 1) * H >= start]
        if path.startswith('public/funding-rate-history'):
            after = int(path.split('after=')[1].split('&')[0]) if 'after=' in path else None
            last = (self.clock['now'] // (8 * H)) * 8 * H - self.funding_lag.get(inst, 0) * 8 * H
            out, t = [], last
            while t >= start and len(out) < 100:
                if after is None or t < after:
                    out.append(dict(fundingTime=str(t), fundingRate=repr(.0001 * (1 + (t // H) % 3)), realizedRate=''))
                t -= 8 * H
            return out
        if path.startswith('market/tickers'):
            return [dict(instId=i, last=repr(price(i, self.clock['now'] // H * H))) for i in COINS]
        raise AssertionError(f'unexpected request {path}')


def universe_doc(price_off=None):
    return dict(version=D.UNIVERSE_VERSION, chosen_at=HOUR0 - 30 * 24 * H, rule='test', instruments=list(COINS),
                specs={i: [.01, 1.0, 1.0] for i in COINS}, price_off=price_off or {})


def clock_fn(clock):
    return lambda: clock['now']


def sleeper(clock):
    def sleep(s):
        clock['now'] += int(s * 1000)
        clock.setdefault('slept', []).append(s)
    return sleep


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.src = self.tmp / 'paper-data'
        self.data = self.tmp / 'server'
        self.clock = {'now': HOUR0 + 90_000}                  # H:01:30
        mc = mock.patch.object(P, 'MANUAL_CLOSES', {})
        mc.start(); self.addCleanup(mc.stop)

    # a published state that the replay accepts: one seat, entries mocked per bar
    def publish(self, state, dash_last, generation=10, universe=None, dash_generation=None):
        ev = self.src / 'evolve'
        ev.mkdir(parents=True, exist_ok=True)
        state = dict(state, generation=generation)
        (ev / 'state.json').write_text(json.dumps(state))
        (ev / 'universe.json').write_text(json.dumps(universe or universe_doc()))
        (ev / 'dashboard.json').write_text(json.dumps(dict(generation=generation if dash_generation is None else dash_generation,
                                                           updated_at='x', paper=dict(curve=[[dash_last, 10000.0]], equity=10000.0))))

    def simple_state(self, next_hour=True):
        g = random_genome(random.Random(1), 'momentum')
        g.update(direction='both', lookback=11, stop_atr=2.0, target_rr=3.0, max_hold=240, risk=.05, leverage=5, vol_min=.01, vol_max=99, tf=1, trail=0)
        node = dict(id='n1', genes=g, species='momentum', fitness=1.0, qualified=True)
        t0 = HOUR0 - 20 * 24 * H
        acc = dict(from_ts=t0, start_equity=10000.0, next=1,
                   segs=[dict(id='a1', champion='n1', slot=0, genes=fix_genes(dict(g)), species='momentum', from_ts=t0, wind_ts=None, boosts=[], reason='carry')],
                   side_cap_from=t0, risk_rules_from=t0, risk_cap_from=t0, exit_rules_from=t0, trail_risk_from=t0, manual_from={})
        if next_hour:
            acc['next_hour_from'] = t0
        return dict(nodes={'n1': node}, team=[dict(id='n1', since=t0)], leader=None, champion='n1', paper=dict(account=acc, start_equity=10000.0))

    def gated(self):
        gate = mock.patch.object(P, 'entry_gate', lambda node, state=None, streak=None: dict(passed=True, reasons=[]))
        gate.start(); self.addCleanup(gate.stop)

    def main(self, *extra, get=None, http=None, clock=None):
        clock = clock or self.clock
        get = get or FakeOKX(clock)
        argv = ['--data', str(self.data), '--source', str(self.src), '--commit-api', '', '--master-api', '', '--retry', '15', *extra]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(argv, now_fn=clock_fn(clock), sleep=sleeper(clock), get=get, http=http or (lambda u, t=30: (_ for _ in ()).throw(AssertionError(u))))
        self.out = buf.getvalue()
        return rc

    def signals(self, name='signals.json'):
        return json.loads((self.data / 'evolve' / name).read_text())


class CompletenessTests(Base):
    def setUp(self):
        super().setUp()
        self.gated()
        self.publish(self.simple_state(), HOUR0 - 3 * H)

    def test_all_complete_writes_signals_with_provenance(self):
        rc = self.main()
        self.assertEqual(rc, 0, self.out)
        sig = self.signals()
        self.assertEqual(sig['last_bar_ts'], HOUR0 - H)
        self.assertEqual(sig['last_bar'], S.iso(HOUR0 - H))
        self.assertEqual(sig['source'], 'server-hourly'); self.assertEqual(sig['version'], S.VERSION)
        self.assertFalse(sig['shadow']); self.assertFalse(sig['legacy_state'])
        self.assertEqual(sig['state']['generation'], 10)
        self.assertEqual(sig['updated_at'], S.iso(HOUR0 + 90_000))
        self.assertEqual(sig['instruments'], COINS)
        self.assertTrue(FS.valid(sig))                                   # what the trader's ExecStartPre accepts
        self.assertEqual(sig['timing']['attempts'], 1)
        last = json.loads((self.data / 'last_run.json').read_text()); self.assertTrue(last['ok'])
        lines = list((self.data / 'archive').glob('signals-*.jsonl'))
        self.assertEqual(len(lines), 1); self.assertTrue(json.loads(lines[0].read_text().splitlines()[-1])['ok'])

    def test_one_coin_missing_the_bar_fails_loudly_at_the_deadline_and_writes_nothing(self):
        get = FakeOKX(self.clock, lag={'SOL-USDT-SWAP': 1})
        rc = self.main(get=get)
        self.assertEqual(rc, S.EXIT_INCOMPLETE)
        self.assertFalse((self.data / 'evolve' / 'signals.json').exists())
        self.assertIn('SOL-USDT-SWAP', self.out); self.assertIn('K 线不齐', self.out)
        self.assertLessEqual(self.clock['now'], HOUR0 + 5 * 60_000 + 15_000)    # gave up at H:05, not later
        self.assertGreater(len(self.clock['slept']), 5)                          # retried in between
        last = json.loads((self.data / 'last_run.json').read_text())
        self.assertFalse(last['ok']); self.assertEqual(last['code'], S.EXIT_INCOMPLETE)
        arch = (self.data / 'archive').glob('signals-*.jsonl')
        self.assertFalse(json.loads(next(arch).read_text().splitlines()[-1])['ok'])
        # load_series (GitHub's path) would have silently gone back one bar for every coin instead
        series = D.load_series(self.tmp / 'gh', self.clock['now'], get=FakeOKX(dict(self.clock), lag={'SOL-USDT-SWAP': 1}), instruments=COINS)
        self.assertEqual(next(iter(series.values())).ts[-1], HOUR0 - 2 * H)

    def test_unconfirmed_last_bar_is_incomplete(self):
        rc = self.main(get=FakeOKX(self.clock, unconfirmed={'XRP-USDT-SWAP'}))
        self.assertEqual(rc, S.EXIT_INCOMPLETE)
        self.assertIn('XRP-USDT-SWAP', self.out)
        self.assertFalse((self.data / 'evolve' / 'signals.json').exists())

    def test_retry_until_complete(self):
        get = FakeOKX(self.clock, lag={'ETH-USDT-SWAP': 1}, heal_after={'ETH-USDT-SWAP': 2})
        rc = self.main(get=get)
        self.assertEqual(rc, 0, self.out)
        sig = self.signals()
        self.assertEqual(sig['last_bar_ts'], HOUR0 - H); self.assertEqual(sig['timing']['attempts'], 3)
        # only the incomplete coin was fetched again
        again = [p for p in get.calls if p.startswith('market/candles')]
        self.assertEqual(sum('ETH-USDT-SWAP' in p for p in again), 3); self.assertEqual(sum('BTC-USDT-SWAP' in p for p in again), 1)

    def test_failed_fetch_is_incomplete_not_dropped(self):
        rc = self.main(get=FakeOKX(self.clock, fail={'DOGE-USDT-SWAP'}))
        self.assertEqual(rc, S.EXIT_INCOMPLETE)
        self.assertIn('DOGE-USDT-SWAP', self.out)

    def test_stopped_coin_is_left_out_like_load_series(self):
        rc = self.main(get=FakeOKX(self.clock, lag={'DOGE-USDT-SWAP': 10}))
        self.assertEqual(rc, 0, self.out)
        sig = self.signals()
        self.assertNotIn('DOGE-USDT-SWAP', sig['instruments']); self.assertEqual(sig['dropped'], ['DOGE-USDT-SWAP'])
        self.assertTrue(any('DOGE' in w for w in sig['warnings']))

    def test_started_after_the_deadline_gets_a_bounded_budget(self):
        clock = {'now': HOUR0 + 20 * 60_000}
        self.assertEqual(self.main(clock=clock, get=FakeOKX(clock, lag={'SOL-USDT-SWAP': 1})), S.EXIT_INCOMPLETE)
        self.assertLessEqual(clock['now'], HOUR0 + 20 * 60_000 + 210_000)
        self.assertGreater(clock['now'], HOUR0 + 20 * 60_000 + 180_000)
        clock = {'now': HOUR0 + 20 * 60_000}
        self.assertEqual(self.main(clock=clock), 0)

    def test_deadline_is_configurable(self):
        rc = self.main('--deadline', '2', get=FakeOKX(self.clock, lag={'SOL-USDT-SWAP': 1}))
        self.assertEqual(rc, S.EXIT_INCOMPLETE)
        self.assertLessEqual(self.clock['now'], HOUR0 + 2 * 60_000 + 15_000)

    def test_funding_record_waited_for_then_warned(self):
        # target bar on a funding hour (00/08/16 UTC): the record on it must be there; one coin's record is late
        hour = (HOUR0 // (8 * H) + 1) * 8 * H + H            # H:01:30 of the hour after a settlement
        clock = {'now': hour + 90_000}
        self.publish(self.simple_state(), hour - 3 * H)
        get = FakeOKX(clock, funding_lag={'ETH-USDT-SWAP': 1})
        rc = self.main(clock=clock, get=get)
        self.assertEqual(rc, 0, self.out)                    # not a failure: an interval change must not stop trading
        sig = self.signals()
        self.assertTrue(any('funding' in w and 'ETH-USDT-SWAP' in w for w in sig['warnings']))
        self.assertGreaterEqual(clock['now'], hour + 8 * 60_000 - 15_000)   # waited until about H:08
        self.assertTrue(S.funding_missing([[0, 1], [8 * H, 1]], 16 * H))
        self.assertFalse(S.funding_missing([[0, 1], [8 * H, 1]], 15 * H))
        self.assertFalse(S.funding_missing([[0, 1], [4 * H, 1], [8 * H, 1]], 8 * H))
        self.assertTrue(S.funding_missing([[0, 1], [H, 1]], 2 * H))         # a 1 h interval coin


class DeadlineTests(Base):
    """A stuck request must not outlast the deadline: a clock running at real speed, budgets of 1–2 s."""

    def setUp(self):
        super().setUp()
        self.gated()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def offset_clock(self, minute=20.0):
        """now_fn running at real speed from HOUR0 + minute."""
        t = time.monotonic()
        return lambda: int(HOUR0 + minute * 60_000 + (time.monotonic() - t) * 1000)

    def stalling(self, when):
        fake = FakeOKX({'now': HOUR0 + 20 * 60_000})

        def get(path):
            if when(path):
                self.release.wait(30)                      # one stuck request, ignores every timeout
                raise RuntimeError('released')
            return fake(path)
        return get

    def go(self, *extra, get=None, now_fn=None, http=None):
        argv = ['--data', str(self.data), '--source', str(self.src), '--commit-api', '', '--master-api', '', '--retry', '0.2', *extra]
        buf = io.StringIO()
        with redirect_stdout(buf):
            t = time.monotonic()
            rc = S.main(argv, now_fn=now_fn, get=get, **({'http': http} if http else {}))
            took = time.monotonic() - t
        self.out = buf.getvalue()
        return rc, took

    def no_signals(self):
        self.assertFalse((self.data / 'evolve' / 'signals.json').exists())
        self.assertFalse(list(self.data.glob('evolve/*')))
        self.assertFalse(json.loads((self.data / 'last_run.json').read_text())['ok'])

    def test_deadline_times(self):
        args = S.parser().parse_args([])
        m = 60_000
        self.assertEqual(S.deadlines(args, HOUR0 + 90_000), (HOUR0 + 5 * m, HOUR0 + 8 * m))            # the timer
        self.assertEqual(S.deadlines(args, HOUR0 + 5 * m), (HOUR0 + 5 * m + 210_000,) * 2)             # late: own budget
        self.assertEqual(S.deadlines(args, HOUR0 + 59 * m), (HOUR0 + H, HOUR0 + H))                    # never into the next hour
        # script deadline < watchdog < TimeoutStartSec, for the timer's start
        svc = unit('okx-signals.service')['Service']
        watchdog = 8 * 60 + S.HARD_GRACE_S
        self.assertLess(watchdog, 90 + int(svc['TimeoutStartSec'][0]))
        self.assertLess(210 + S.HARD_GRACE_S, int(svc['TimeoutStartSec'][0]))                          # a late start as well

    def test_a_stuck_candle_request_is_cut_at_the_deadline(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H)
        now = self.offset_clock()
        rc, took = self.go('--late-budget', '1.5', now_fn=now, get=self.stalling(lambda p: p.startswith('market/') and 'SOL-' in p))
        self.assertEqual(rc, S.EXIT_INCOMPLETE, self.out)
        self.assertGreater(took, 1.2); self.assertLess(took, 10)
        self.assertIn('SOL-USDT-SWAP: fetch failed: aborted at the deadline', self.out)
        self.no_signals()

    def test_the_retry_loop_ends_at_the_deadline(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H)
        with mock.patch.object(S, 'LAST_TRY_S', .2):
            rc, took = self.go('--late-budget', '1.5', now_fn=self.offset_clock(), get=FakeOKX({'now': HOUR0 + 20 * 60_000}, lag={'SOL-USDT-SWAP': 1}))
        self.assertEqual(rc, S.EXIT_INCOMPLETE)
        self.assertGreater(took, .9); self.assertLess(took, 10)
        self.no_signals()

    def test_complete_only_after_the_deadline_is_a_failure(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H)
        clock = {'now': HOUR0 + 4 * 60_000}

        class Slow(FakeOKX):
            def __call__(s, path):
                if path.startswith('market/candles') and 'DOGE' in path:
                    clock['now'] = HOUR0 + 5 * 60_000 + 1        # everything arrives, one ms too late
                return super().__call__(path)
        self.assertEqual(self.main(clock=clock, get=Slow(clock)), S.EXIT_INCOMPLETE)
        self.no_signals()

    def test_a_stuck_funding_request_is_cut_and_the_cached_rates_are_used(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H)
        now = self.offset_clock()
        stuck = lambda p: p.startswith('public/funding') and 'ETH-' in p
        fast = FakeOKX({'now': HOUR0 + 20 * 60_000})
        D.update_funding(self.data / 'candles', 'ETH-USDT-SWAP', now(), fast)          # cached by an earlier run
        rc, took = self.go('--late-budget', '2', now_fn=now, get=self.stalling(stuck))
        self.assertEqual(rc, 0, self.out)                                               # its semantics: warn, do not fail
        timing = self.signals()['timing']
        self.assertGreater(took, 1.5); self.assertLess(timing['data_s'], 5, timing)     # the cut itself; the replay and the write follow it
        self.assertTrue(any('ETH-USDT-SWAP' in w and 'funding' in w for w in self.signals()['warnings']))

    def test_no_funding_rates_at_all_at_the_deadline_fails(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H)
        now = self.offset_clock()
        rc, took = self.go('--late-budget', '1.5', now_fn=now, get=self.stalling(lambda p: p.startswith('public/funding') and 'ETH-' in p))
        self.assertEqual(rc, S.EXIT_INCOMPLETE, self.out)
        self.assertLess(took, 10); self.assertIn('ETH-USDT-SWAP', self.out)
        self.no_signals()

    def test_okx_get_cuts_a_hanging_curl_and_its_silent_retries(self):
        for script, label in (('exec sleep 60', 'one request that hangs'), ('exit 7', 'fast failures: 1 + 2 + 3 + 4 s of retry sleeps')):
            fake = self.tmp / f'bin-{len(script)}'
            fake.mkdir()
            (fake / 'curl').write_text(f'#!/bin/sh\n{script}\n'); (fake / 'curl').chmod(0o755)
            with mock.patch.dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}"):
                t = time.monotonic()
                with self.assertRaises(D.DeadlineExceeded, msg=label):
                    OKX_GET('market/candles?instId=X', deadline=t + 1.0)
                took = time.monotonic() - t
                self.assertGreater(took, .9, label); self.assertLess(took, 10, label)
                with self.assertRaises(D.DeadlineExceeded):                            # nothing is started after it
                    OKX_GET('market/candles?instId=X', deadline=time.monotonic() - 1)
                with self.assertRaises(RuntimeError):
                    S.curl_text('http://127.0.0.1:1/x', .3)

    def test_okx_get_without_a_deadline_is_unchanged(self):
        out = mock.Mock(stdout=json.dumps(dict(code='0', data=[1])))
        with mock.patch.object(D.subprocess, 'run', return_value=out) as run:
            self.assertEqual(OKX_GET('x'), [1])
        self.assertNotIn('timeout', run.call_args[1])                                  # GitHub's run.py: no new limit
        self.assertEqual(run.call_args[0][0][:4], ['curl', '-sS', '-m', '20'])

    def script(self, *extra, patch='', curl='exec sleep 60', timeout=30):
        """The real command line in its own process (os._exit, watchdog), every curl hanging."""
        fake = self.tmp / 'hang'
        fake.mkdir(exist_ok=True)
        (fake / 'curl').write_text(f'#!/bin/sh\n{curl}\n'); (fake / 'curl').chmod(0o755)
        argv = ['--data', str(self.data), '--source', str(self.src), '--commit-api', '', '--master-api', '', '--deadline', '0',
                '--funding-deadline', '0', *extra]
        cmd = [sys.executable, str(ROOT / 'tools' / 'evolve' / 'signals_hourly.py'), *argv]
        if patch:
            cmd = [sys.executable, '-c', f'import sys, os, time; sys.path.insert(0, {str(ROOT / "tools" / "evolve")!r}); import signals_hourly as S\n'
                                         f'{patch}\nrc = S.main({argv!r}, watchdog=True); sys.stdout.flush(); os._exit(rc)']
        t = time.monotonic()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}"))
        return p, time.monotonic() - t

    def test_the_process_itself_exits_2_on_time_when_every_request_hangs(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H)
        p, took = self.script('--late-budget', '2')
        self.assertEqual(p.returncode, S.EXIT_INCOMPLETE, p.stdout + p.stderr)
        self.assertLess(took, 10)                                                       # start-up + 2 s, not 4 × 20 s of retries
        self.assertIn('K 线不齐', p.stdout)
        self.no_signals()

    def test_the_watchdog_ends_a_run_stuck_outside_the_requests(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H)
        p, took = self.script('--late-budget', '1', patch='S.HARD_GRACE_S = 1\nS.produce = lambda *a, **k: time.sleep(60)')
        self.assertEqual(p.returncode, S.EXIT_INCOMPLETE, p.stdout + p.stderr)
        self.assertLess(took, 10); self.assertIn('看门狗', p.stdout)
        self.no_signals()

    def test_nothing_is_written_once_the_watchdog_has_fired(self):
        done = threading.Event()
        dog = S.Watchdog(.01, lambda: None, exit_fn=lambda rc: done.set())
        self.assertTrue(done.wait(5)); self.assertFalse(dog.disarm())
        dog = S.Watchdog(30, lambda: self.fail('fired'), exit_fn=lambda rc: self.fail('exit'))
        self.assertTrue(dog.disarm()); self.assertTrue(dog.timer.finished.is_set())

    def test_a_hanging_github_falls_back_to_the_local_copy_in_time(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H)
        self.assertEqual(self.main(), 0)
        seen = []

        def http(url, timeout=30):
            seen.append(timeout)
            raise RuntimeError('timed out')
        now = HOUR0 + 90_000
        with redirect_stdout(io.StringIO()):
            rc = S.main(['--data', str(self.data), '--source', 'https://raw.githubusercontent.com/o/r/paper-data', '--commit-api', 'https://api.github.com/x'],
                        now_fn=lambda: now, sleep=lambda s: None, get=FakeOKX({'now': now}), http=http)
        self.assertEqual(rc, 0); self.assertFalse(self.signals()['state']['fetched'])
        self.assertTrue(all(0 < t <= S.SOURCE_BUDGET_S for t in seen)); self.assertLessEqual(seen[-1], S.MASTER_BUDGET_S)
        late = S.bound_http(http, now - 1, lambda: now)
        seen.clear()
        with self.assertRaises(RuntimeError):
            late('https://x', 30)
        self.assertEqual(seen, [])                                                     # nothing starts after the limit

    def test_cache_files_are_replaced_atomically(self):
        p = self.tmp / 'c' / 'X.json'
        D._write_cache(p, '{"rows": []}')
        with mock.patch.object(D.os, 'replace', side_effect=OSError('killed')):
            with self.assertRaises(OSError):
                D._write_cache(p, '{"rows": [[1')
        self.assertEqual(json.loads(p.read_text()), {'rows': []})


class SourceTests(Base):
    def setUp(self):
        super().setUp()
        self.gated()

    def test_generation_or_last_bar_regress_uses_the_local_copy(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H, generation=10)
        self.assertEqual(self.main(), 0)
        self.publish(self.simple_state(), HOUR0 - 7 * H, generation=9)      # older copy from the CDN
        self.clock['now'] += H
        self.assertEqual(self.main(), 0, self.out)
        sig = self.signals()
        self.assertEqual(sig['state']['generation'], 10); self.assertEqual(sig['state']['regressed_from']['generation'], 9)
        self.assertIn('比本地旧', self.out)
        self.publish(self.simple_state(), HOUR0 - 7 * H, generation=10)     # same generation, last published bar went back
        self.clock['now'] += H
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.signals()['state']['prev_last'], HOUR0 - 3 * H)
        self.publish(self.simple_state(), HOUR0 + H, generation=11)         # newer: taken and kept
        self.clock['now'] += H
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.signals()['state']['generation'], 11)
        self.assertEqual(json.loads((self.data / 'source' / 'meta.json').read_text())['generation'], 11)

    def test_mixed_cdn_copy_or_failed_download_uses_the_local_copy(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H, generation=10)
        self.assertEqual(self.main(), 0)
        self.publish(self.simple_state(), HOUR0 - 3 * H, generation=12, dash_generation=11)
        self.assertEqual(self.main(), 0)
        self.assertFalse(self.signals()['state']['fetched']); self.assertEqual(self.signals()['state']['generation'], 10)
        (self.src / 'evolve' / 'state.json').unlink()
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.signals()['state']['generation'], 10)

    def test_no_source_and_no_local_copy_fails(self):
        self.assertEqual(self.main(), S.EXIT_SOURCE)
        self.assertFalse((self.data / 'evolve' / 'signals.json').exists())

    def test_http_source_resolves_one_commit_and_skips_an_unchanged_one(self):
        self.publish(self.simple_state(), HOUR0 - 3 * H)
        sha = 'a' * 40
        seen = []

        def http(url, timeout=30):
            seen.append(url)
            if url.startswith('https://api.github.com/'):
                return json.dumps(dict(sha=sha))
            m = re.fullmatch(r'https://raw\.githubusercontent\.com/o/r/([0-9a-f]{40})/evolve/(\w+\.json)', url)
            self.assertTrue(m, url); self.assertEqual(m.group(1), sha)
            return (self.src / 'evolve' / m.group(2)).read_text()
        argv = ['--data', str(self.data), '--source', 'https://raw.githubusercontent.com/o/r/paper-data', '--commit-api', 'https://api.github.com/x',
                '--master-api', '']
        with redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(argv, now_fn=clock_fn(self.clock), sleep=sleeper(self.clock), get=FakeOKX(self.clock), http=http), 0)
        self.assertEqual(len(seen), 4)                                       # api + three files at that commit
        self.assertEqual(self.signals()['state']['commit'], sha)
        seen.clear()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(argv, now_fn=clock_fn(self.clock), sleep=sleeper(self.clock), get=FakeOKX(self.clock), http=http), 0)
        self.assertEqual(len(seen), 1)                                       # same commit: no download

    def test_state_drift_or_pre_rule_state_fails_unless_shadow_legacy(self):
        self.publish(self.simple_state(next_hour=False), HOUR0 - 3 * H)
        self.assertEqual(self.main(), S.EXIT_SOURCE)
        self.assertIn('next-hour', self.out)
        self.assertFalse((self.data / 'evolve' / 'signals.json').exists())
        self.assertEqual(self.main('--legacy-state'), S.EXIT_SOURCE)        # never for the trader's file
        self.assertEqual(self.main('--shadow', '--legacy-state'), 0)
        self.assertTrue(self.signals('signals.shadow.json')['legacy_state'])
        st = self.simple_state()                                             # seats disagree with team: only GitHub may fix
        st['team'] = [dict(id='n2', since=0)]
        st['nodes']['n2'] = dict(st['nodes']['n1'], id='n2')
        self.publish(st, HOUR0 - 3 * H, generation=11)
        self.assertEqual(self.main(), S.EXIT_SOURCE)
        self.assertIn('disagree', self.out)


class OutputTests(Base):
    def setUp(self):
        super().setUp()
        self.gated()
        self.publish(self.simple_state(), HOUR0 - 3 * H)

    def test_shadow_writes_only_its_own_paths(self):
        self.assertEqual(self.main('--shadow'), 0)
        self.assertFalse((self.data / 'evolve' / 'signals.json').exists())
        sig = self.signals('signals.shadow.json')
        self.assertTrue(sig['shadow'])
        self.assertTrue(list((self.data / 'shadow').glob('signals-*.jsonl')))
        self.assertFalse((self.data / 'archive').exists())
        self.clock['now'] += H
        self.assertEqual(self.main('--shadow'), 0)
        lines = sum(len(f.read_text().splitlines()) for f in (self.data / 'shadow').glob('signals-*.jsonl'))
        self.assertEqual(lines, 2)                                           # one line per hour

    def test_as_of_needs_shadow_and_lines_up_with_an_older_bar(self):
        self.assertEqual(self.main('--as-of', S.iso(HOUR0 - 2 * H + 90_000)), S.EXIT_SOURCE)
        self.assertEqual(self.main('--shadow', '--as-of', S.iso(HOUR0 - 2 * H + 90_000)), 0, self.out)
        self.assertEqual(self.signals('signals.shadow.json')['last_bar_ts'], HOUR0 - 3 * H)

    def test_atomic_write_keeps_the_old_file_on_failure_and_leaves_no_tmp(self):
        p = self.tmp / 'x' / 'signals.json'
        S.atomic_write(p, '{"a": 1}')
        with mock.patch.object(S.os, 'replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                S.atomic_write(p, '{"a": 2}')
        self.assertEqual(json.loads(p.read_text()), {'a': 1})
        self.assertEqual([f.name for f in p.parent.iterdir()], ['signals.json'])
        self.assertEqual(oct(p.stat().st_mode & 0o777), '0o644')

    def test_a_crash_writes_no_signals(self):
        with mock.patch.object(P, 'update', side_effect=ZeroDivisionError('boom')):
            with mock.patch('sys.stderr', io.StringIO()):
                self.assertEqual(self.main(), S.EXIT_ERROR)
        self.assertFalse((self.data / 'evolve' / 'signals.json').exists())

    def test_lock_held_exits_4(self):
        self.data.mkdir(parents=True)
        with open(self.data / '.signals.lock', 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.main(), S.EXIT_LOCKED)
        self.assertEqual(self.main(), 0)

    def test_no_credentials_no_env_files_no_auth(self):
        """Runs with keys in the environment: reads only its own files, sends no key anywhere, never opens an env file."""
        opened = []
        real_open = builtins.open

        def spy(file, *a, **k):
            opened.append(str(file))
            return real_open(file, *a, **k)
        env = dict(os.environ, OKX_API_KEY='k-should-not-leak', OKX_API_SECRET='s', OKX_API_PASSPHRASE='p')
        get = FakeOKX(self.clock)
        with mock.patch.dict(os.environ, env), mock.patch('builtins.open', spy), mock.patch('io.open', spy):
            self.assertEqual(self.main(get=get), 0)
        for f in opened:
            if f.isdigit():                                              # os.fdopen of the atomic write's own tmp file
                continue
            self.assertTrue(f.startswith((str(self.tmp), str(ROOT))) or '/.git/' in f or f.startswith('/proc') or 'python' in f, f)
            self.assertNotIn('okx-live.env', f); self.assertNotIn('/etc/', f)
        self.assertFalse(any('k-should-not-leak' in p for p in get.calls))
        self.assertNotIn('k-should-not-leak', (self.data / 'evolve' / 'signals.json').read_text())
        src = (ROOT / 'tools' / 'evolve' / 'signals_hourly.py').read_text()
        for word in ('os.environ', 'getenv', 'OKX_API', 'EnvironmentFile', 'Authorization', 'okx-live.env', 'x-simulated'):
            self.assertNotIn(word, src)
        # no evolution / publication call (in the code, not the docstring that lists what it does not call)
        import ast
        tree = ast.parse(src)
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        imports = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        self.assertFalse({'refresh', 'generation', 'choose_team', 'price_check', 'prune'} & attrs)
        self.assertNotIn('evolution', imports); self.assertNotIn('E', names)
        self.assertNotIn('git', [c.value for c in ast.walk(tree) if isinstance(c, ast.Constant) and isinstance(c.value, str)])
        # the public GET used for GitHub carries no credentials
        with mock.patch.object(S.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='{}')) as run:
            S.curl_text('https://example.invalid/x')
        cmd = run.call_args[0][0]
        self.assertNotIn('-K', cmd); self.assertFalse(any('Authorization' in c or 'OK-ACCESS' in c for c in cmd))

    def test_replay_never_changes_the_state(self):
        before = (self.src / 'evolve' / 'state.json').read_text()
        self.assertEqual(self.main(), 0)
        self.assertEqual((self.src / 'evolve' / 'state.json').read_text(), before)
        self.assertEqual(json.loads((self.data / 'source' / 'state.json').read_text()), json.loads(before))


class DeterminismTests(Base):
    """The hourly replay gives the signals the GitHub side computes from the same input."""

    def entries(self, bars):
        """Mocked strategy signals: entries on the given bar timestamps (so the test has open positions)."""
        def signal_sets(g, series):
            s = next(iter(series.values()))
            out = {}
            for k, (ts, inst, side) in enumerate(bars):
                if ts in s.ts:
                    i = s.ts.index(ts)
                    out.setdefault(i, []).append((inst, side, series[inst].c[i] * .03))
            return out, {}
        return mock.patch.object(X, 'signal_sets', side_effect=signal_sets)

    def test_same_bar_same_signals_as_paper_update_github_side(self):
        self.gated()
        bars = [(HOUR0 - 6 * H, 'ETH-USDT-SWAP', 1), (HOUR0 - 4 * H, 'SOL-USDT-SWAP', -1), (HOUR0 - H, 'BTC-USDT-SWAP', 1)]
        with self.entries(bars):
            # GitHub at H−1:57 (last bar H−2 h) publishes; the server replays at H:01:30 (last bar H−1 h)
            gh_clock = {'now': HOUR0 - 3 * 60_000}
            state = self.simple_state()
            (self.tmp / 'gh' / 'evolve').mkdir(parents=True)
            (self.tmp / 'gh' / 'evolve' / 'universe.json').write_text(json.dumps(universe_doc()))
            gh = D.load_series(self.tmp / 'gh', gh_clock['now'], get=FakeOKX(gh_clock))
            P.update(state, gh, [], {}, now=gh_clock['now'])
            self.publish(state, HOUR0 - 2 * H)
            self.assertEqual(self.main(), 0, self.out)
            server = self.signals()
            # the next GitHub run on the same data (last bar H − 1 h), without changes
            gh2_clock = {'now': HOUR0 + 50 * 60_000}
            gh2 = D.load_series(self.tmp / 'gh', gh2_clock['now'], get=FakeOKX(gh2_clock))
            self.assertEqual(next(iter(gh2.values())).ts[-1], HOUR0 - H)
            st2 = json.loads(json.dumps(state))
            view, sig2 = P.update(st2, gh2, [], {}, now=gh2_clock['now'])
        self.assertTrue(server['positions'])
        self.assertEqual(server['positions'], json.loads(json.dumps(sig2)))
        self.assertEqual(server['exits'], view['exits'])
        self.assertEqual(server['equity'], view['equity'])

    def test_run_py_end_to_end_same_input_same_signals(self):
        """GitHub's run.py (real evolution, 1 generation) publishes; the server replays the published files at the same
        hour and the next hour: positions / exits / equity identical (rank aside the next hour, PR1 contract)."""
        gh_clock = {'now': HOUR0 - 20 * 60_000}                              # GitHub at H−1:40 → last bar H − 2 h
        ghdata = self.tmp / 'gh'
        (ghdata / 'evolve').mkdir(parents=True)
        (ghdata / 'evolve' / 'universe.json').write_text(json.dumps(universe_doc()))
        fake = FakeOKX(gh_clock)
        defaults = D.load_series.__defaults__, D.price_check.__defaults__, D.okx_get
        D.load_series.__defaults__ = (fake, None, None)
        D.price_check.__defaults__ = (fake, fake)
        clock = R.time.time
        self.addCleanup(setattr, R.time, 'time', clock)

        def restore():
            D.load_series.__defaults__, D.price_check.__defaults__, D.okx_get = defaults
        self.addCleanup(restore)
        R.time.time = lambda: gh_clock['now'] / 1000
        with redirect_stdout(io.StringIO()):
            R.main(['--data', str(ghdata), '--generations', '1'])
        R.time.time = clock
        published = json.loads((ghdata / 'evolve' / 'signals.json').read_text())
        dash = json.loads((ghdata / 'evolve' / 'dashboard.json').read_text())
        self.src = ghdata
        clock_s = {'now': HOUR0 - H + 90_000}                                # server H−1:01:30 → last bar H − 2 h
        self.assertEqual(self.main(clock=clock_s), 0, self.out)
        server = self.signals()
        self.assertEqual(server['last_bar_ts'], dash['paper']['curve'][-1][0])
        self.assertEqual(server['positions'], published['positions'])
        self.assertEqual(server['exits'], published['exits'])
        # PRICE_OFF LIVE: both producers publish the live-only list (and keep it out of dashboard.json)
        self.assertEqual(server['price_off_live'], published['price_off_live'])
        self.assertEqual(server['price_off_live_exits'], published['price_off_live_exits'])
        self.assertNotIn('price_off_live', dash['paper'])
        self.assertEqual(server['equity'], dash['paper']['equity'])
        self.assertEqual(server['instruments'], published['instruments'])
        # a second replay of the same input is byte-identical apart from the clock fields
        first = (self.data / 'evolve' / 'signals.json').read_text()
        self.assertEqual(self.main(clock=dict(clock_s)), 0)
        strip = lambda d: {k: v for k, v in json.loads(d).items() if k not in ('updated_at', 'computed_at', 'timing', 'state')}
        self.assertEqual(strip(first), strip((self.data / 'evolve' / 'signals.json').read_text()))


class SignalTimingTests(unittest.TestCase):
    def test_defaults_keep_the_github_4h_mode(self):
        self.assertEqual(odt.signal_timing({}), ('github', 9 * H, 5 * H))
        self.assertEqual(odt.signal_timing({'OKX_SIGNAL_MODE': 'GitHub '}), ('github', 9 * H, 5 * H))
        self.assertEqual((odt.LIVE_SIGNAL_MAX_AGE, odt.OPEN_WINDOW), (9 * H, 5 * H))      # module defaults unchanged

    def test_hourly_mode_and_overrides(self):
        self.assertEqual(odt.signal_timing({'OKX_SIGNAL_MODE': 'hourly'}), ('hourly', 135 * 60_000, 2 * H))
        self.assertEqual(odt.signal_timing({'OKX_SIGNAL_MODE': 'hourly', 'OKX_LIVE_SIGNAL_MAX_AGE_MIN': '150', 'OKX_OPEN_WINDOW_MIN': '90'}),
                         ('hourly', 150 * 60_000, 90 * 60_000))
        self.assertEqual(odt.signal_timing({'OKX_OPEN_WINDOW_MIN': '240'}), ('github', 9 * H, 4 * H))

    def test_bad_values_refuse_to_run(self):
        for env in ({'OKX_SIGNAL_MODE': 'fast'}, {'OKX_LIVE_SIGNAL_MAX_AGE_MIN': 'abc'}, {'OKX_OPEN_WINDOW_MIN': '0'},
                    {'OKX_OPEN_WINDOW_MIN': '5000'}):
            with self.assertRaises(SystemExit):
                odt.signal_timing(env)

    def test_apply_sets_the_globals_the_mirror_uses(self):
        old = odt.LIVE_SIGNAL_MAX_AGE, odt.OPEN_WINDOW
        self.addCleanup(lambda: (setattr(odt, 'LIVE_SIGNAL_MAX_AGE', old[0]), setattr(odt, 'OPEN_WINDOW', old[1])))
        odt.apply_signal_timing({'OKX_SIGNAL_MODE': 'hourly'})
        self.assertEqual((odt.LIVE_SIGNAL_MAX_AGE, odt.OPEN_WINDOW), (135 * 60_000, 2 * H))
        odt.apply_signal_timing({})
        self.assertEqual((odt.LIVE_SIGNAL_MAX_AGE, odt.OPEN_WINDOW), (9 * H, 5 * H))

    def test_open_window_and_late_ok_in_the_mirror(self):
        """An entry 3 h old: followed under the GitHub window (5 h); in hourly mode (2 h) only through late_ok."""
        keep = ('LIVE_SIGNAL_MAX_AGE', 'OPEN_WINDOW', 'INSTRUMENTS', 'RISK', 'SAME_SIDE_CAP', 'ACTIVE_PROFILE', 'DAILY_HALT', 'MARGIN_MODE')
        old = {k: getattr(odt, k) for k in keep}                       # configure_evolved changes the trader's globals
        self.addCleanup(lambda: [setattr(odt, k, v) for k, v in old.items()])
        now = datetime(2026, 11, 22, 10, 2, tzinfo=timezone.utc)
        opened = int(now.timestamp() * 1000) - 3 * H
        paper = {'positions': {'BTC-USDT-SWAP': dict(side=1, entry=100.0, stop=95.0, opened=opened, strategy='evolved')}, 'exits': {}}

        def decide(env, late):
            odt.apply_signal_timing(env)
            odt.configure_evolved(['BTC-USDT-SWAP'])
            m = odt.Mirror.__new__(odt.Mirror)
            m.s = dict(mirrored={}, paused=False, halted=False, day=odt.local_day(now), day_start=1000.0)
            m.paper, m.now, m.now_ms, m.dry, m.actions, m.positions, m.equity = paper, now, int(now.timestamp() * 1000), False, [], {}, 1000.0
            m.load = lambda: None
            m.leader_upl = lambda: 0.0
            m.late_ok = lambda inst, p: late
            m.log = lambda action, inst, **kw: m.actions.append(dict(action=action, inst=inst, **kw))
            m.open = lambda key, inst, p: m.actions.append(dict(action='open', inst=inst))
            m.run('run')
            return [a['action'] for a in m.actions], m.s['mirrored']
        self.assertEqual(decide({}, False)[0], ['open'])
        acts, mirrored = decide({'OKX_SIGNAL_MODE': 'hourly'}, False)
        self.assertEqual(acts, ['skip']); self.assertEqual(next(iter(mirrored.values()))['reason'], 'too_old')
        self.assertEqual(decide({'OKX_SIGNAL_MODE': 'hourly'}, True)[0], ['open'])   # late_ok fallback kept


class FetchSignalsHourlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.src = self.tmp / 'signals' / 'evolve' / 'signals.json'
        self.dest = self.tmp / 'live'

    def write(self, age_min=10, last_bar_ts=HOUR0 - H, **kw):
        now = datetime.now(timezone.utc).timestamp() * 1000
        doc = dict(updated_at=S.iso(int(now - age_min * 60_000)), instruments=['BTC-USDT-SWAP'], positions={}, exits={},
                   source='server-hourly', shadow=False, legacy_state=False, last_bar_ts=last_bar_ts, last_bar=S.iso(last_bar_ts))
        doc.update(kw)
        self.src.parent.mkdir(parents=True, exist_ok=True)
        self.src.write_text(json.dumps(doc))
        return doc

    def run_fs(self, mode='hourly', action=None):
        env = {'OKX_SIGNAL_MODE': mode, 'OKX_HOURLY_SIGNALS': str(self.src)}
        with redirect_stdout(io.StringIO()):
            FS.main(['--data', str(self.dest), *(['--action', action] if action else [])], env=env)
        return json.loads((self.dest / 'evolve' / 'signals.json').read_text())

    def test_copies_a_fresh_hourly_file(self):
        doc = self.write()
        self.assertEqual(self.run_fs(), doc)

    def test_refuses_stale_shadow_legacy_or_github_files(self):
        for kw in (dict(age_min=140), dict(shadow=True), dict(legacy_state=True), dict(source=None), dict(updated_at='2099-01-01T00:00:00Z')):
            self.write(**kw)
            with self.assertRaises(SystemExit):
                self.run_fs()
            self.assertFalse((self.dest / 'evolve' / 'signals.json').exists())
        self.write(age_min=130)
        self.run_fs()                                                            # 2 h 10 min: still inside 2 h 15 min

    def test_a_stale_file_stops_run_but_not_pause_or_snapshot(self):
        old = self.write(last_bar_ts=HOUR0 - 2 * H)
        self.run_fs()
        for bad in (dict(age_min=140), dict(shadow=True)):
            self.write(**bad)
            for action in (None, 'run', 'resume', 'follow-current', 'flatten'):
                with self.assertRaises(SystemExit, msg=action):
                    self.run_fs(action=action)
            for action in ('pause', 'snapshot'):
                self.assertEqual(self.run_fs(action=action), old)                  # goes on, the bad file is not copied
        self.src.unlink()
        for action in ('pause', 'snapshot'):
            self.assertEqual(self.run_fs(action=action), old)                      # no file at all
        fresh = self.write()
        self.assertEqual(self.run_fs(action='pause'), fresh)                       # a good file is still copied first

    def test_github_mode_ignores_the_action(self):
        for action in ('run', 'pause', 'snapshot'):
            with mock.patch.object(FS, 'fetch', side_effect=SystemExit('download failed')):
                with self.assertRaises(SystemExit):
                    FS.main(['--data', str(self.dest), '--action', action], env={})

    def test_keeps_a_newer_local_copy(self):
        self.write(last_bar_ts=HOUR0)
        self.run_fs()
        self.write(last_bar_ts=HOUR0 - H)
        self.assertEqual(self.run_fs()['last_bar_ts'], HOUR0)

    def test_github_mode_is_unchanged_and_default(self):
        sig = dict(updated_at='2026-11-21T20:54:31Z', instruments=['BTC-USDT-SWAP'], positions={})
        for env in ({}, {'OKX_SIGNAL_MODE': 'github'}):
            with mock.patch.object(FS, 'fetch', return_value=sig) as f, redirect_stdout(io.StringIO()):
                FS.main(['--data', str(self.dest)], env=env)
            f.assert_called_once()
            self.assertEqual(json.loads((self.dest / 'evolve' / 'signals.json').read_text()), sig)
        with self.assertRaises(SystemExit):
            FS.main(['--data', str(self.dest)], env={'OKX_SIGNAL_MODE': 'bogus'})


def unit(path):
    """systemd unit → {section: {key: [values]}} (comments dropped)."""
    out, sec = {}, None
    for line in (ROOT / 'deploy' / 'live' / path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('['):
            sec = out.setdefault(line.strip('[]'), {})
            continue
        k, v = line.split('=', 1)
        sec.setdefault(k, []).append(v)
    return out


class SystemdUnitTests(unittest.TestCase):
    def test_signals_service(self):
        for name, hooks in (('okx-signals.service', True), ('okx-signals-shadow.service', False)):
            u = unit(name)
            svc = u['Service']
            self.assertEqual(svc['Type'], ['oneshot'])
            if hooks:
                self.assertEqual(u['Unit']['OnSuccess'], ['okx-live@run.service'])
                self.assertEqual(u['Unit']['OnFailure'], ['okx-live@run.service'])
                self.assertNotIn('--shadow', svc['ExecStart'][0])
            else:
                self.assertNotIn('OnSuccess', u['Unit']); self.assertNotIn('OnFailure', u['Unit'])
                self.assertIn('--shadow', svc['ExecStart'][0])
            self.assertIn('tools/evolve/signals_hourly.py', svc['ExecStart'][0])
            self.assertEqual(svc['MemoryMax'], ['520M']); self.assertEqual(svc['MemoryHigh'], ['460M'])
            self.assertLess(int(svc['CPUWeight'][0]), 100)                    # below the default 100 of the live trader
            self.assertGreater(int(svc['Nice'][0]), 0)
            self.assertIn('IOWeight', svc)
            self.assertEqual(svc['User'], ['modi-signals'])                   # not the live trader's user
            self.assertEqual(svc['StateDirectory'], ['modi-okx-signals'])
            self.assertNotIn('EnvironmentFile', svc)
            self.assertIn('-/etc/modi-okx-live', svc['InaccessiblePaths'])
            self.assertNotIn('okx-live.env', '\n'.join(v for vs in svc.values() for v in vs))
            self.assertEqual(svc['ProtectSystem'], ['strict'])
            self.assertIn('memory.peak', svc['ExecStopPost'][0])              # MemoryPeak is gone once a oneshot is inactive
            self.assertNotIn('%%%%', svc['ExecStopPost'][0])

    def test_timers(self):
        for name, target in (('okx-signals.timer', 'okx-signals.service'), ('okx-signals-shadow.timer', 'okx-signals-shadow.service')):
            t = unit(name)['Timer']
            self.assertEqual(t['OnCalendar'], ['*-*-* *:01:30 UTC'])
            self.assertEqual(t['Unit'], [target])
            self.assertEqual(t['AccuracySec'], ['1s'])
        self.assertEqual(unit('okx-live.timer')['Timer']['OnCalendar'], ['*-*-* *:50:00 UTC'])   # the heartbeat stays

    def test_live_unit_unchanged_and_hourly_dropin(self):
        live = unit('okx-live@.service')['Service']
        self.assertEqual(live['ExecStartPre'], ['/usr/bin/python3 tools/fetch_signals.py --data /var/lib/modi-okx-live'])
        self.assertNotIn('OKX_SIGNAL_MODE', '\n'.join(live.get('Environment', [])))   # default = github signals
        drop = unit('okx-live-hourly.conf')['Service']
        # only the drop-in passes the action; the empty assignment first clears the base unit's command (exactly one is left)
        self.assertEqual(drop['ExecStartPre'], ['', live['ExecStartPre'][0] + ' --action %i'])
        self.assertIn('--action %i', live['ExecStart'][0])
        stop = unit('okx-live-stop.service')['Service']
        self.assertNotIn('fetch_signals', json.dumps(stop))                          # flatten + pause never needs signals
        self.assertIn('OKX_SIGNAL_MODE=hourly', drop['Environment'])
        self.assertIn('OKX_HOURLY_SIGNALS=/var/lib/modi-okx-signals/evolve/signals.json', drop['Environment'])
        self.assertGreater(int(drop['CPUWeight'][0]), int(unit('okx-signals.service')['Service']['CPUWeight'][0]))
        self.assertLess(int(drop['OOMScoreAdjust'][0]), 0)


class CompareSignalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))

    def pos(self, **kw):
        return dict(dict(side=1, entry=100.0, stop=95.0, tp=115.0, opened=HOUR0, dist=5.0, trailing=False, champion='n1', rank=1,
                         risk_factor=1.0, seat_from=0, winding=False, risk=.05, leverage=5), **kw)

    def shadow_line(self, bar, positions, equity=10100.0, **kw):
        return dict(ok=True, last_bar_ts=bar, last_bar=S.iso(bar), computed_at=S.iso(bar + H + 100_000), positions=positions, exits={},
                    equity=equity, team=['n1'], champion='n1', timing=dict(latency_s=100.0, total_s=30.0), state=dict(generation=5), **kw)

    def gh_dir(self, name, bar, positions, equity=10100.0):
        d = self.tmp / name / 'evolve'
        d.mkdir(parents=True)
        (d / 'signals.json').write_text(json.dumps(dict(updated_at=S.iso(bar + 2 * H), positions=positions, exits={}, team=['n1'], champion='n1')))
        (d / 'dashboard.json').write_text(json.dumps(dict(generation=5, paper=dict(curve=[[bar, equity]], equity=equity))))
        return str(d.parent)

    def run_cs(self, shadow_lines, dirs):
        f = self.tmp / 'shadow' / 'signals-2026-11-22.jsonl'
        f.parent.mkdir(exist_ok=True)
        f.write_text(''.join(json.dumps(x) + '\n' for x in shadow_lines))
        js = self.tmp / 'rep.json'
        argv = ['--shadow', str(f.parent), '--json', str(js), '--out', str(self.tmp / 'rep.md')]
        for d in dirs:
            argv += ['--github-dir', d]
        rc = CS.main(argv)
        return rc, json.loads(js.read_text())

    def test_match_mismatch_rank_and_stats(self):
        b0, b1 = HOUR0, HOUR0 + 4 * H
        lines = [self.shadow_line(b0, {'BTC-USDT-SWAP': self.pos()})] + \
                [self.shadow_line(b0 + k * H, {'BTC-USDT-SWAP': self.pos()}) for k in (1, 2, 3)] + \
                [self.shadow_line(b1, {'BTC-USDT-SWAP': self.pos(rank=2), 'ETH-USDT-SWAP': self.pos(opened=b1)}),
                 dict(ok=False, code=2, error='K 线不齐')]
        dirs = [self.gh_dir('g0', b0, {'BTC-USDT-SWAP': self.pos()}),
                self.gh_dir('g1', b1, {'BTC-USDT-SWAP': self.pos(rank=1), 'ETH-USDT-SWAP': self.pos(opened=b1)})]
        rc, rep = self.run_cs(lines, dirs)
        self.assertEqual(rc, 0)
        self.assertEqual((rep['aligned'], rep['matched'], rep['rank_only']), (2, 2, 1))
        self.assertEqual(rep['rows'][1]['events']['server_open'], [f'ETH-USDT-SWAP|{b1}'])
        self.assertEqual(rep['failures'], {'2': 1}); self.assertEqual(rep['success_rate'], round(5 / 6, 4))
        self.assertEqual(rep['latency_s']['p50'], 100.0); self.assertEqual(rep['missing_hours'], [])
        self.assertIn('一致', (self.tmp / 'rep.md').read_text())
        # a different stop is a mismatch, and so is an equity gap or a missing coin
        dirs2 = [self.gh_dir('h0', b0, {'BTC-USDT-SWAP': self.pos(stop=94.0)})]
        rc, rep = self.run_cs(lines, dirs2)
        self.assertEqual(rc, 1); self.assertEqual(rep['rows'][0]['position_diff'], {'BTC-USDT-SWAP': {'stop': [95.0, 94.0]}})
        rc, rep = self.run_cs(lines, [self.gh_dir('h1', b0, {'BTC-USDT-SWAP': self.pos()}, equity=10000.0)])
        self.assertEqual(rc, 1); self.assertFalse(rep['rows'][0]['equity_ok'])
        rc, rep = self.run_cs(lines, [self.gh_dir('h2', b0, {})])
        self.assertEqual(rc, 1); self.assertEqual(rep['rows'][0]['position_diff'], {'BTC-USDT-SWAP': {'present': [True, False]}})

    def test_field_missing_on_one_side(self):
        old = {k: v for k, v in self.pos().items() if k not in ('risk_factor', 'seat_from')}   # GitHub before the fields existed
        lines = [self.shadow_line(HOUR0, {'BTC-USDT-SWAP': self.pos()})]
        rc, rep = self.run_cs(lines, [self.gh_dir('m0', HOUR0, {'BTC-USDT-SWAP': old})])
        self.assertEqual(rc, 1); self.assertEqual(rep['rows'][0]['field_missing'], {'BTC-USDT-SWAP': ['risk_factor', 'seat_from']})
        self.assertEqual(rep['rows'][0]['position_diff'], {})
        f = self.tmp / 'shadow'
        rc = CS.main(['--shadow', str(f), '--github-dir', str(self.tmp / 'm0'), '--ignore-missing-fields', '--out', str(self.tmp / 'r.md')])
        self.assertEqual(rc, 0)

    def test_nothing_aligned_exits_2_and_missing_hours(self):
        lines = [self.shadow_line(HOUR0, {}), self.shadow_line(HOUR0 + 2 * H, {})]
        rc, rep = self.run_cs(lines, [self.gh_dir('g', HOUR0 + 7 * H, {})])
        self.assertEqual(rc, 2); self.assertEqual(rep['missing_hours'], [S.iso(HOUR0 + H)])

    def test_github_api_source(self):
        shas = ['b' * 40]

        def http(url, timeout=60):
            if url.startswith('https://api.github.com/'):
                return json.dumps([dict(sha=s) for s in shas])
            name = url.rsplit('/', 1)[1]
            if name == 'signals.json':
                return json.dumps(dict(updated_at='x', positions={}, exits={}))
            return json.dumps(dict(generation=1, paper=dict(curve=[[HOUR0, 1.0]], equity=1.0)))
        snaps = CS.load_github_api(1, http=http)
        self.assertEqual([(s['label'], s['last_bar_ts']) for s in snaps], [('bbbbbbb', HOUR0)])


if __name__ == '__main__':
    unittest.main()

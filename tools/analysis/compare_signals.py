#!/usr/bin/env python3
"""Reconcile the server's hourly signals (shadow archive) with the signals.json GitHub publishes to paper-data.

Shadow run (docs/shadow-run-plan.md): the server writes one signals document per hour (signals_hourly.py --shadow →
<data>/shadow/signals-YYYY-MM-DD.jsonl). GitHub publishes evolve/signals.json every 4 h; its last bar is the last
point of the paper curve in the dashboard.json of the same commit. Both are aligned BY LAST BAR and compared:

  positions   per coin: present on both sides, and every field equal (side, entry, stop, tp, opened, dist, trailing,
              champion, risk_factor, seat_from, winding, risk / leverage …). `rank` is reported apart (PR1 contract:
              rank follows the latest team / fitness and may differ between an older and a newer state.json); a field
              present on one side only (other code version) is listed apart and is a mismatch unless --ignore-missing-fields;
  open / close signals   the inst|opened keys that appear / disappear since the previous aligned bar, on each side;
  exits       {inst|opened: reason} equal;
  equity      server equity vs dashboard.json paper.equity (tolerance --equity-tol, default 0.01 USDT);
  team / champion   informational (the server may still have the previous state.json; changes are pending anyway).

Plus, from the archive alone: success rate (ok lines / hours seen), failures by exit code, missing hours, latency
(computed_at − bar close) and run time percentiles.

Sources for the GitHub side (any mix, repeatable):
  --github-api N          the last N commits of paper-data via the public GitHub API + raw files at each commit
  --github-git REPO [--ref origin/paper-data] [-n N]   a local clone (git fetch first): git show <sha>:evolve/…
  --github-dir DIR        a directory with evolve/signals.json and evolve/dashboard.json (one snapshot)
Shadow side: --shadow PATH… (jsonl archives, directories of them, or single signals*.json files).

Exit code: 0 every aligned bar matches (rank aside), 1 a mismatch, 2 nothing to align. --json writes the full report.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOUR = 3600_000
REPO = 'modi-cjh/crypto-panel'
INFO_FIELDS = ('rank',)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse_ts(s):
    return int(datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp() * 1000)


def curl(url, timeout=60):
    out = subprocess.run(['curl', '-sS', '-f', '-m', str(timeout), '-H', 'User-Agent: modi-compare', url], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f'GET {url} failed (curl {out.returncode})')
    return out.stdout


# ---- loading ------------------------------------------------------------------------------------------------------
def load_shadow(paths):
    """(ok docs, failure lines) from archives / files."""
    files = []
    for p in map(Path, paths):
        files += sorted(p.glob('signals-*.jsonl')) + sorted(p.glob('*.json')) if p.is_dir() else [p]
    ok, bad = [], []
    for f in files:
        lines = f.read_text().splitlines() if f.suffix == '.jsonl' else [f.read_text()]
        for line in lines:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get('ok') is False:
                bad.append(d)
            elif isinstance(d.get('last_bar_ts'), int):
                ok.append(d)
    return ok, bad


def github_snapshot(signals, dashboard, label):
    curve = ((dashboard or {}).get('paper') or {}).get('curve') or []
    if not curve:
        return None
    return dict(label=label, last_bar_ts=int(curve[-1][0]), signals=signals, equity=(dashboard.get('paper') or {}).get('equity'),
                generation=dashboard.get('generation'), updated_at=signals.get('updated_at'))


def load_github_dir(d):
    d = Path(d)
    return github_snapshot(json.loads((d / 'evolve' / 'signals.json').read_text()), json.loads((d / 'evolve' / 'dashboard.json').read_text()), str(d))


def load_github_git(repo, ref, n):
    shas = subprocess.run(['git', '-C', str(repo), 'log', '--format=%H', f'-n{n}', ref], capture_output=True, text=True, check=True).stdout.split()
    out = []
    for sha in shas:
        try:
            show = lambda f: json.loads(subprocess.run(['git', '-C', str(repo), 'show', f'{sha}:evolve/{f}'], capture_output=True, text=True, check=True).stdout)
            snap = github_snapshot(show('signals.json'), show('dashboard.json'), sha[:7])
        except (subprocess.CalledProcessError, ValueError):
            continue
        if snap:
            out.append(snap)
    return out


def load_github_api(n, http=curl, repo=REPO, branch='paper-data'):
    commits = json.loads(http(f'https://api.github.com/repos/{repo}/commits?sha={branch}&per_page={min(int(n), 100)}', 30))
    out = []
    for c in commits:
        sha = c['sha']
        base = f'https://raw.githubusercontent.com/{repo}/{sha}/evolve'
        try:
            snap = github_snapshot(json.loads(http(f'{base}/signals.json')), json.loads(http(f'{base}/dashboard.json')), sha[:7])
        except (RuntimeError, ValueError):
            continue
        if snap:
            out.append(snap)
    return out


# ---- comparing ----------------------------------------------------------------------------------------------------
def diff_positions(a, b, tol=0.0):
    """({inst: {field: (server, github)}} for coins on one side only or with a different field, rank diffs,
    fields present on one side only ({inst: [field]}: a producer of another code version, e.g. GitHub before a field existed))."""
    out, ranks, missing = {}, {}, {}
    for inst in sorted(set(a) | set(b)):
        if inst not in a or inst not in b:
            out[inst] = {'present': (inst in a, inst in b)}
            continue
        d = {}
        for k in sorted(set(a[inst]) | set(b[inst])):
            x, y = a[inst].get(k), b[inst].get(k)
            if (k in a[inst]) != (k in b[inst]):
                missing.setdefault(inst, []).append(k)
                continue
            if k in INFO_FIELDS:
                if x != y:
                    ranks[inst] = (x, y)
                continue
            if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool) and tol:
                if abs(x - y) > tol * max(1.0, abs(x), abs(y)):
                    d[k] = (x, y)
            elif x != y:
                d[k] = (x, y)
        if d:
            out[inst] = d
    return out, ranks, missing


def keys(sig):
    return {f"{i}|{int(p['opened'])}" for i, p in (sig.get('positions') or {}).items()}


def pct(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def compare(shadow, failures, github, tol=0.0, equity_tol=0.01, ignore_missing=False):
    by_bar = {}
    for d in shadow:                                    # several runs for one bar (manual reruns): the latest one
        if d['last_bar_ts'] not in by_bar or d.get('computed_at', '') >= by_bar[d['last_bar_ts']].get('computed_at', ''):
            by_bar[d['last_bar_ts']] = d
    gh = {}
    for g in github:
        gh.setdefault(g['last_bar_ts'], g)
    rows, prev = [], None
    for bar in sorted(set(by_bar) & set(gh)):
        s, g = by_bar[bar], gh[bar]
        pd, ranks, missing = diff_positions(s.get('positions') or {}, g['signals'].get('positions') or {}, tol)
        ex = s.get('exits') or {}, g['signals'].get('exits') or {}
        exit_diff = {k: (ex[0].get(k), ex[1].get(k)) for k in sorted(set(ex[0]) | set(ex[1])) if ex[0].get(k) != ex[1].get(k)}
        eq = (s.get('equity'), g.get('equity'))
        eq_ok = eq[0] is not None and eq[1] is not None and abs(eq[0] - eq[1]) <= equity_tol
        events = None
        if prev:
            ps, pg = prev
            events = dict(server_open=sorted(keys(s) - keys(ps)), server_close=sorted(keys(ps) - keys(s)),
                          github_open=sorted(keys(g['signals']) - keys(pg['signals'])), github_close=sorted(keys(pg['signals']) - keys(g['signals'])))
        events_ok = events is None or (events['server_open'] == events['github_open'] and events['server_close'] == events['github_close'])
        rows.append(dict(bar=iso(bar), github=g['label'], github_generation=g.get('generation'), server_generation=(s.get('state') or {}).get('generation'),
                         positions=len(s.get('positions') or {}), github_positions=len(g['signals'].get('positions') or {}),
                         position_diff=pd, rank_diff=ranks, field_missing=missing, exit_diff=exit_diff, equity=eq, equity_ok=eq_ok, events=events,
                         team_same=s.get('team') == g['signals'].get('team'), champion_same=s.get('champion') == g['signals'].get('champion'),
                         match=not pd and not exit_diff and eq_ok and events_ok and (ignore_missing or not missing)))
        prev = (s, g)
    lat = [d['timing']['latency_s'] for d in by_bar.values() if (d.get('timing') or {}).get('latency_s') is not None]
    tot = [d['timing']['total_s'] for d in by_bar.values() if (d.get('timing') or {}).get('total_s') is not None]
    bars = sorted(by_bar)
    missing = []
    if bars:
        missing = [iso(t) for t in range(bars[0], bars[-1] + HOUR, HOUR) if t not in by_bar]
    codes = {}
    for f in failures:
        codes[str(f.get('code'))] = codes.get(str(f.get('code')), 0) + 1
    runs = len(shadow) + len(failures)
    return dict(aligned=len(rows), matched=sum(r['match'] for r in rows), rank_only=sum(1 for r in rows if r['match'] and r['rank_diff']),
                rows=rows, server_bars=len(by_bar), github_bars=len(gh), first_bar=iso(bars[0]) if bars else None, last_bar=iso(bars[-1]) if bars else None,
                missing_hours=missing, runs=runs, ok_runs=len(shadow), failures=codes,
                success_rate=round(len(shadow) / runs, 4) if runs else None,
                latency_s=dict(p50=pct(lat, .5), p90=pct(lat, .9), max=max(lat) if lat else None),
                total_s=dict(p50=pct(tot, .5), p90=pct(tot, .9), max=max(tot) if tot else None),
                warnings=sorted({w for d in by_bar.values() for w in d.get('warnings') or []}))


def markdown(rep):
    out = ['# 每小时信号对账（服务器影子 vs GitHub 发布）', '',
           f"- 对齐的 K 线：{rep['aligned']}，一致 {rep['matched']}（其中只有 rank 不同 {rep['rank_only']}）",
           f"- 服务器影子：{rep['server_bars']} 根（{rep['first_bar']} → {rep['last_bar']}），运行 {rep['runs']} 次，成功率 {rep['success_rate']}，"
           f"失败 {rep['failures'] or '无'}，缺失小时 {len(rep['missing_hours'])}",
           f"- 收盘后延迟（秒）p50 {rep['latency_s']['p50']} / p90 {rep['latency_s']['p90']} / 最大 {rep['latency_s']['max']}；"
           f"单次用时 p50 {rep['total_s']['p50']} / 最大 {rep['total_s']['max']}", '']
    if rep['warnings']:
        out += ['警告：'] + [f'- {w}' for w in rep['warnings']] + ['']
    out += ['| K 线 | GitHub | 结果 | 仓位（服务器/GitHub） | 权益（服务器/GitHub） | 差异 |', '|---|---|---|---|---|---|']
    for r in rep['rows']:
        diff = []
        if r['position_diff']:
            diff.append('positions ' + json.dumps(r['position_diff'], ensure_ascii=False)[:300])
        if r['exit_diff']:
            diff.append('exits ' + json.dumps(r['exit_diff'], ensure_ascii=False)[:200])
        if r['rank_diff']:
            diff.append('rank ' + json.dumps(r['rank_diff']))
        if r['field_missing']:
            fields = sorted({f for fs in r['field_missing'].values() for f in fs})
            diff.append(f"一侧缺字段 {fields}（{len(r['field_missing'])} 个币）")
        if not r['equity_ok']:
            diff.append('equity')
        if r['events'] and not r['match']:
            diff.append('events ' + json.dumps(r['events'])[:200])
        if not r['team_same']:
            diff.append('team(info)')
        out.append(f"| {r['bar']} | {r['github']} g{r['github_generation']} | {'一致' if r['match'] else '**不一致**'} | "
                   f"{r['positions']}/{r['github_positions']} | {r['equity'][0]}/{r['equity'][1]} | {'; '.join(diff) or '—'} |")
    return '\n'.join(out) + '\n'


def main(argv=None, http=curl):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--shadow', nargs='+', required=True)
    ap.add_argument('--github-api', type=int)
    ap.add_argument('--github-git')
    ap.add_argument('--ref', default='origin/paper-data')
    ap.add_argument('-n', type=int, default=30)
    ap.add_argument('--github-dir', action='append', default=[])
    ap.add_argument('--tol', type=float, default=0.0, help='relative tolerance for numeric position fields (default exact)')
    ap.add_argument('--equity-tol', type=float, default=0.01)
    ap.add_argument('--ignore-missing-fields', action='store_true',
                    help='a field present on one side only (other code version) is reported but not a mismatch')
    ap.add_argument('--json', type=Path)
    ap.add_argument('--out', type=Path, help='markdown report (default stdout)')
    a = ap.parse_args(argv)
    shadow, failures = load_shadow(a.shadow)
    github = [s for s in (load_github_dir(d) for d in a.github_dir) if s]
    if a.github_git:
        github += load_github_git(a.github_git, a.ref, a.n)
    if a.github_api:
        github += load_github_api(a.github_api, http=http)
    rep = compare(shadow, failures, github, a.tol, a.equity_tol, a.ignore_missing_fields)
    text = markdown(rep)
    if a.out:
        a.out.write_text(text)
    else:
        print(text)
    if a.json:
        a.json.write_text(json.dumps(rep, indent=1, ensure_ascii=False))
    if not rep['aligned']:
        return 2
    return 0 if rep['matched'] == rep['aligned'] else 1


if __name__ == '__main__':
    sys.exit(main())

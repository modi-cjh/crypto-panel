#!/usr/bin/env python3
"""OKX live account anomaly check (box side, standard library only, read-only).

Runs on the dev box right after tools/live_sync.py in the hourly "OKX 实盘看板同步" job. It reads the live server's
okx_live/summary.json and the tail of actions.jsonl over ssh (same read-only path as live_sync: `sudo -n -u modi-live
cat / tail`, nothing is written on the server) and checks:

  stale        summary.updated_at older than STALE_HOURS (service stopped / timer not running)
  halted       summary.halted (incl. the 25% daily-loss halt)
  paused       summary.paused (manual pause / stop)
  day_loss     (equity - day_start) / day_start < -DAY_LOSS_LIMIT
  stop_streak  the newest STOP_STREAK closes were all stop-outs (or liquidations)
  api_error    an `error` action (order / close / leverage / stop placement failed) within ERROR_WINDOW_MIN minutes
  read_error   the server could not be read at all (ssh / JSON failure)

It never sends anything. It prints one JSON object on stdout and sets the exit code; the caller (Grok Bot in the
hourly job) forwards `alerts` to the user only when `notify` is true.

  {"ok": false, "notify": true, "checked_at": "...Z",
   "alerts":     [{"code": "halted", "msg": "...", "fingerprint": "halted"}, ...],   every active anomaly
   "new_alerts": [...],                       the active anomalies not reported in the previous run (dedup)
   "resolved":   ["stale", ...]}              codes active last run and gone now (informational, never notified)

Exit codes: 0 no anomaly · 1 anomaly with something new → notify · 2 anomaly, all already reported → stay silent.

Dedup: the state file (default /workspace/modi/.live_alert_state.json, `--state ''` disables) keeps the fingerprints
active in the last run. An alert is new when its fingerprint was not active last run, so a condition that persists is
reported once; a condition that clears and comes back, a new stop / error event (fingerprint carries its timestamp) or a
new day's loss (fingerprint carries the date) is reported again.

Usage:
  python3 tools/live_alert.py                                  # read the live server, dedup via the default state file
  python3 tools/live_alert.py --summary s.json --actions a.jsonl --state ''     # local files (tests / drills)

Output never contains credentials: only whitelisted fields, and error details pass live_sync's secret check.
"""
import argparse
import datetime as dt
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_sync as ls  # noqa: E402

STALE_HOURS = 2
DAY_LOSS_LIMIT = 0.10
STOP_STREAK = 3
ERROR_WINDOW_MIN = 90
DEFAULT_STATE = '/workspace/modi/.live_alert_state.json'
CLOSE_ACTIONS = ('close', 'closed_on_exchange')
STOP_OUTCOMES = ('stop', 'liquidation')


def parse_ts(value):
    s = ls.stamp(value)
    return dt.datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=dt.timezone.utc) if s else None


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def alert(code, msg, key=None):
    return {'code': code, 'msg': msg, 'fingerprint': f'{code}:{key}' if key else code}


def safe_text(value, limit=80):
    """Short, single-line exchange text, or None if it looks anything like a credential."""
    if not isinstance(value, str):
        return None
    text = ' '.join(value.split())[:limit]
    return text if text and not ls.secret_problems(text) else None


def close_kind(a):
    """'stop' / 'other' for a close action, None when not yet known (enrichment arrives in a later run)."""
    outcome, reason = a.get('outcome'), a.get('reason')
    if outcome in STOP_OUTCOMES or (isinstance(reason, str) and 'stop' in reason):
        return 'stop'
    if outcome or (a['action'] == 'close' and reason):
        return 'other'
    pnl = ls.number(a.get('pnl'))
    if a['action'] == 'closed_on_exchange' and pnl is not None:
        return 'stop' if pnl < 0 else 'other'          # same fallback as okx_demo_trader.outcome()
    return None


def evaluate(summary, actions, now):
    alerts = []
    updated = parse_ts(summary.get('updated_at'))
    if updated is None:
        alerts.append(alert('stale', 'summary.json 缺少有效的 updated_at，无法判断实盘服务是否在运行'))
    elif now - updated > dt.timedelta(hours=STALE_HOURS):
        hours = (now - updated).total_seconds() / 3600
        alerts.append(alert('stale', f'实盘数据已 {hours:.1f} 小时未更新（最后 {iso(updated)}），服务可能停了或定时没跑'))

    day = (updated or now).strftime('%Y-%m-%d')
    if summary.get('halted') is True:
        alerts.append(alert('halted', '实盘已熔断停手（halted=true，可能触发了当日亏损 25% 停手）', day))
    if summary.get('paused') is True:
        alerts.append(alert('paused', '实盘已暂停（paused=true），不会开新仓'))

    equity, start = ls.number(summary.get('equity')), ls.number(summary.get('day_start'))
    if equity is not None and start and start > 0:
        pct = (equity - start) / start
        if pct < -DAY_LOSS_LIMIT:
            alerts.append(alert('day_loss', f'当日亏损 {pct * 100:.2f}%（权益 {equity:.2f} / 日初 {start:.2f} USDT），'
                                            f'超过 {DAY_LOSS_LIMIT * 100:.0f}%', day))

    # summary.recent_actions carries the outcome / pnl that the trader fills in after the fact; merge like live_sync.
    extra = {}
    for a in summary.get('recent_actions') or []:
        if isinstance(a, dict):
            extra[(a.get('ts'), a.get('action'), a.get('inst'))] = a
    rows = []
    for a in actions:
        if isinstance(a, dict) and parse_ts(a.get('ts')) and isinstance(a.get('action'), str):
            e = extra.get((a.get('ts'), a.get('action'), a.get('inst'))) or {}
            rows.append(dict(e, **{k: v for k, v in a.items() if v is not None}))
    rows.sort(key=lambda a: parse_ts(a['ts']))

    streak = []
    for a in reversed(rows):
        if a['action'] not in CLOSE_ACTIONS or a.get('dry_run') is True:
            continue
        kind = close_kind(a)
        if kind is None:
            continue
        if kind != 'stop':
            break
        streak.append(a)
    if len(streak) >= STOP_STREAK:
        names = '、'.join(ls.inst(a.get('inst')) or '?' for a in streak[:STOP_STREAK])
        alerts.append(alert('stop_streak', f'最近连续 {len(streak)} 笔平仓都是止损（最新：{names}）',
                            ls.stamp(streak[0]['ts'])))

    cutoff = now - dt.timedelta(minutes=ERROR_WINDOW_MIN)
    errors = [a for a in rows if a['action'] == 'error' and parse_ts(a['ts']) >= cutoff]
    if errors:
        last = errors[-1]
        what = ls.word(last.get('step')) or '未知步骤'
        detail = safe_text(last.get('detail'))
        msg = (f'过去 {ERROR_WINDOW_MIN} 分钟有 {len(errors)} 次下单 / API 报错，'
               f'最近一次 {ls.stamp(last["ts"])} {ls.inst(last.get("inst")) or ""} {what}'
               + (f'：{detail}' if detail else ''))
        alerts.append(alert('api_error', msg, ls.stamp(last['ts'])))
    return alerts


# ---- dedup state ------------------------------------------------------------------------------
def load_state(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return {fp: c for fp, c in (data.get('active') or {}).items() if isinstance(fp, str) and isinstance(c, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def save_state(path, alerts, now):
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.live_alert_', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump({'checked_at': iso(now), 'active': {a['fingerprint']: a['code'] for a in alerts}}, f, ensure_ascii=False)
    os.replace(tmp, path)


def report(alerts, now, state_path=None):
    previous = load_state(state_path) if state_path else {}
    new = [a for a in alerts if a['fingerprint'] not in previous]
    active_codes = {a['code'] for a in alerts}
    resolved = sorted({c for c in previous.values() if c not in active_codes})
    if state_path:
        save_state(state_path, alerts, now)
    result = {'ok': not alerts, 'notify': bool(new), 'checked_at': iso(now),
              'alerts': alerts, 'new_alerts': new, 'resolved': resolved}
    return result, (0 if not alerts else 1 if new else 2)


def read_local(summary_path, actions_path):
    try:
        with open(summary_path, encoding='utf-8') as f:
            summary = json.load(f)
        text = ''
        if actions_path:
            with open(actions_path, encoding='utf-8') as f:
                text = f.read()
    except OSError as e:
        raise ls.SyncError(f'无法读取本地文件：{e.strerror}')
    except ValueError:
        raise ls.SyncError('summary.json 不是有效 JSON')
    return summary, text


def main(argv=None, now=None):
    ap = argparse.ArgumentParser(description='OKX 实盘异常检查（只读服务器，输出 JSON，不发送消息）')
    ap.add_argument('--host', default=ls.DEFAULT_HOST)
    ap.add_argument('--key', default=ls.DEFAULT_KEY)
    ap.add_argument('--remote-dir', default=ls.DEFAULT_REMOTE_DIR)
    ap.add_argument('--remote-user', default=ls.DEFAULT_USER)
    ap.add_argument('--summary', help='本地 summary.json（演练 / 测试用，不连服务器）')
    ap.add_argument('--actions', help='本地 actions.jsonl（配合 --summary）')
    ap.add_argument('--state', default=DEFAULT_STATE, help=f'去重状态文件（默认 {DEFAULT_STATE}；空字符串 = 不去重）')
    ap.add_argument('--now', help='当前时间（ISO UTC，演练用）')
    args = ap.parse_args(argv)
    now = now or (parse_ts(args.now) if args.now else None) or dt.datetime.now(dt.timezone.utc)
    try:
        if args.summary:
            summary, text = read_local(args.summary, args.actions)
        else:
            summary, _state, text = ls.read_server(args.host, args.key, args.remote_dir, args.remote_user)
        if not isinstance(summary, dict):
            raise ls.SyncError('summary.json 不是 JSON 对象')
        alerts = evaluate(summary, ls.parse_actions(text), now)
    except ls.SyncError as e:
        alerts = [alert('read_error', f'无法读取实盘数据：{e}')]
    result, code = report(alerts, now, args.state or None)
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if ls.secret_problems(text):           # belt and braces: never print anything credential-shaped
        result = {'ok': False, 'notify': True, 'checked_at': iso(now), 'resolved': [],
                  'alerts': [alert('read_error', '告警内容未通过密钥检查，已隐藏；请登录服务器查看')]}
        result['new_alerts'] = result['alerts']
        text, code = json.dumps(result, ensure_ascii=False, indent=1), 1
    print(text)
    return code


if __name__ == '__main__':
    sys.exit(main())

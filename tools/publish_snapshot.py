#!/usr/bin/env python3
"""定时生成公开快照并推送到 GitHub Pages 分支。只提交 paper/*.json，不读写交易程序。

用法（在运行模拟盘的机器上，仓库目录内）：
    python3 tools/publish_snapshot.py                 # 每 10 分钟一次，直到 Ctrl+C
    python3 tools/publish_snapshot.py --once          # 只发布一次
    python3 tools/publish_snapshot.py --dry-run --once  # 生成并本地提交，不推送
未识别的参数原样传给 make_snapshot.py，例如 --source-root /workspace/crypto-bot。
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = 'paper'
# GitHub Pages 经典构建软上限约每小时 10 次；快照每次都会更新 generated_at。
MIN_INTERVAL = 360


def log(message):
    print(f'[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] {message}', flush=True)


def git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], check=check, capture_output=True, text=True)


def public_files(repo):
    return sorted(str(p.relative_to(repo)) for p in (Path(repo) / PUBLIC).glob('*.json'))


def push_with_retry(repo, branch, delays=(2, 4, 8, 16), sleep=time.sleep):
    for attempt, delay in enumerate((0, *delays)):
        if delay:
            sleep(delay)
        result = git(repo, 'push', 'origin', f'HEAD:{branch}', check=False)
        if result.returncode == 0:
            return True
        log(f'推送失败（第 {attempt + 1} 次）：{result.stderr.strip().splitlines()[-1:] or ["未知错误"]}')
    return False


def publish_once(repo, branch, generator, push=True, now=None, sleep=time.sleep):
    """返回 'published' / 'committed' / 'unchanged' / 'skipped'。失败时保留上次已发布的快照。"""
    repo = Path(repo)
    current = git(repo, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    if current != branch:
        raise SystemExit(f'当前分支是 {current}，请先切换到 {branch} 再发布。')
    if push:
        # 先同步远程代码改动（如合并的 PR）；本脚本只改 paper/，正常不会冲突。
        pulled = git(repo, 'pull', '--rebase', '--autostash', 'origin', branch, check=False)
        if pulled.returncode != 0:
            git(repo, 'rebase', '--abort', check=False)
            log('同步远程失败，本轮跳过；请手动检查仓库状态。')
            return 'skipped'
    generated = subprocess.run(generator, cwd=repo, capture_output=True, text=True)
    if generated.returncode != 0:
        # make_snapshot 校验失败时不写文件；不转发原始异常内容。
        log('快照生成或校验失败，保留上次发布的数据。')
        return 'skipped'
    files = public_files(repo)
    if not files:
        log('没有可发布的 paper/*.json。')
        return 'skipped'
    git(repo, 'add', '--', *files)
    if git(repo, 'diff', '--cached', '--quiet', '--', *files, check=False).returncode == 0:
        log('快照无变化。')
        return 'unchanged'
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ')
    # 用 pathspec 限定提交范围，其他已暂存的改动不会被带进快照提交。
    git(repo, 'commit', '-m', f'snapshot {stamp}', '--', *files)
    if not push:
        log(f'已本地提交 snapshot {stamp}（未推送）。')
        return 'committed'
    if push_with_retry(repo, branch, sleep=sleep):
        log(f'已发布 snapshot {stamp}。')
        return 'published'
    log('推送多次失败，提交保留在本地，下轮会一起推送。')
    return 'committed'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--interval', type=int, default=600, help=f'发布间隔秒数，默认 600，最小 {MIN_INTERVAL}')
    parser.add_argument('--branch', default='master', help='GitHub Pages 发布分支，默认 master')
    parser.add_argument('--once', action='store_true', help='只发布一次后退出')
    parser.add_argument('--dry-run', action='store_true', help='生成并本地提交，但不拉取、不推送')
    args, passthrough = parser.parse_known_args(argv)
    if args.interval < MIN_INTERVAL:
        parser.error(f'--interval 不能小于 {MIN_INTERVAL} 秒，以免超出 GitHub Pages 构建频率限制。')
    generator = [sys.executable, str(ROOT / 'tools' / 'make_snapshot.py'), *passthrough]
    while True:
        started = time.monotonic()
        publish_once(ROOT, args.branch, generator, push=not args.dry_run)
        if args.once:
            return
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('已停止发布。')

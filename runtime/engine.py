import fcntl
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT, credentials_ready

sys.path.insert(0, str(ROOT / 'tools'))
from runtime_guard import atomic_json

LEGACY_WORKFLOWS = ('paper-trading.yml', 'start-aggressive-demo.yml', 'okx-demo-smoke.yml', 'stop-demo.yml')


class Authority:
    def __init__(self, repository):
        self.repository, self.checked = repository, None

    def read(self, suffix):
        headers = {'Accept':'application/vnd.github+json','User-Agent':'crypto-panel-demo-runtime'}
        token = os.environ.get('PANEL_GITHUB_READ_TOKEN')
        if token: headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request('https://api.github.com/repos/' + self.repository + suffix, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def ensure(self):
        ttl = 30 if os.environ.get('PANEL_GITHUB_READ_TOKEN') else 240
        if self.checked is not None and time.monotonic() - self.checked < ttl:
            return
        ids = set()
        workflows = self.read('/actions/workflows?per_page=100').get('workflows',[])
        for name in LEGACY_WORKFLOWS:
            workflow = next((w for w in workflows if w.get('path','').split('/')[-1]==name),{})
            if workflow.get('state') not in ('disabled_manually', 'disabled_inactivity'):
                raise RuntimeError('旧 GitHub 交易/控制任务尚未停用，禁止接管执行权')
            ids.add(workflow['id'])
        for status in ('in_progress', 'queued', 'waiting'):
            runs = self.read('/actions/runs?status=' + status + '&per_page=100').get('workflow_runs', [])
            if any(r.get('workflow_id') in ids for r in runs):
                raise RuntimeError('旧交易任务仍在运行或排队，禁止同时执行')
        self.checked = time.monotonic()


class Runner:
    def __init__(self, config, latch):
        self.config, self.latch = config, latch

    def run(self, stage):
        args = {
            'scan': ['paper_trader.py', '--data', str(self.config.data)],
            'execute': ['okx_demo_trader.py', '--data', str(self.config.data), '--action', 'run'],
            'snapshot': ['okx_demo_trader.py', '--data', str(self.config.data), '--action', 'snapshot'],
            'stop': ['stop_demo.py', '--data', str(self.config.data)],
        }[stage]
        env = dict(os.environ, PANEL_RUNTIME_CONTROL_FILE=str(self.latch), PANEL_RUNTIME_DB=str(self.config.private/'runtime.sqlite3'), PYTHONUNBUFFERED='1')
        log_dir = self.config.private / 'logs'; log_dir.mkdir(exist_ok=True, mode=0o700)
        # Logs stay outside the web root and never become public API responses.
        log_path = log_dir / (time.strftime('%Y%m%d') + '.log')
        with log_path.open('a') as log:
            log_path.chmod(0o600)
            log.write(f'\n{time.time():.0f} stage={stage}\n'); log.flush()
            result = subprocess.run([sys.executable, str(ROOT / 'tools' / args[0]), *args[1:]], env=env, cwd=ROOT,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=900 if stage == 'scan' else 180)
        if result.returncode:
            raise RuntimeError(stage + '执行失败，请检查服务器私有日志')


class Engine:
    def __init__(self, config, store, runner=None, authority=None, ready=None):
        self.config, self.store = config, store
        self.latch = config.private / 'control.json'
        self.runner = runner or Runner(config, self.latch)
        self.authority = authority or Authority(config.repository)
        self.ready = ready or credentials_ready
        self.wake, self.shutdown = threading.Event(), threading.Event()
        self.worker = self.pulse = None
        self.lock = None
        self.next_cycle = 0

    def sync_latch(self, cancel=False):
        atomic_json(self.latch, dict(paused=self.store.get('paused', True), cancel_scan=cancel))

    def ensure_ready(self):
        if not self.config.enabled: raise RuntimeError('常驻交易执行尚未启用')
        if not self.ready(): raise RuntimeError('服务器尚未配置 OKX 模拟交易密钥')
        if not all((self.config.data / p).is_file() for p in ('combined/state.json','okx_demo/state.json')):
            raise RuntimeError('尚未导入原账户状态；禁止创建替代账户')
        self.authority.ensure()

    def start(self):
        self.lock = (self.config.private / 'executor.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close(); self.lock = None
            raise RuntimeError('同一数据目录已有常驻执行器')
        self.store.recover()
        if not self.store.get('paused', True):
            try: self.ensure_ready()
            except Exception:
                self.store.set(paused=True, last_error='重启后执行权或密钥检查未通过，已暂停')
        self.sync_latch(cancel=(self.store.command() or {}).get('state') in ('queued','running'))
        self.worker = threading.Thread(target=self.loop, name='demo-executor', daemon=True)
        self.pulse = threading.Thread(target=self.heartbeat, name='demo-heartbeat', daemon=True)
        self.worker.start(); self.pulse.start()

    def stop(self):
        self.shutdown.set(); self.wake.set()
        atomic_json(self.latch, dict(paused=True, cancel_scan=True))
        if self.worker: self.worker.join(timeout=185)
        if self.pulse: self.pulse.join(timeout=5)
        self.store.set(worker_alive=False)
        if self.lock:
            fcntl.flock(self.lock, fcntl.LOCK_UN); self.lock.close(); self.lock = None

    def heartbeat(self):
        while not self.shutdown.is_set():
            self.store.set(heartbeat=time.time(), worker_alive=bool(self.worker and self.worker.is_alive()))
            self.shutdown.wait(2)

    def submit(self, action, idem):
        if action not in ('stop','resume'): raise ValueError('Unsupported action')
        if not self.config.enabled: raise ValueError('执行器未启用；先完成服务器迁移与执行权检查')
        command = self.store.enqueue(action, idem)
        if action == 'stop' and command['state'] in ('queued','running'): self.sync_latch(cancel=True)
        self.wake.set()
        return {k:command[k] for k in ('id','action','state','created','updated','message')}

    def handle(self, command):
        try:
            self.ensure_ready()
            if command['action'] == 'stop':
                self.sync_latch(cancel=True)
                self.runner.run('stop')
                summary = json.loads((self.config.data / 'okx_demo/summary.json').read_text())
                if (summary.get('stop_control') or {}).get('status') != 'completed' or not summary.get('paused'):
                    raise RuntimeError('平仓结果未全部确认，保持暂停')
                self.store.set(paused=True)
                self.store.finish_command(command['id'],'completed','程序持仓已全部平仓并保持暂停')
            else:
                state_path = self.config.data / 'okx_demo/state.json'
                state = json.loads(state_path.read_text())
                if (state.get('stop_control') or {}).get('status') in ('pending','failed'):
                    raise RuntimeError('先确认上一次平仓结果，再恢复交易')
                today=datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d')
                if state.get('halted') and state.get('day')==today:
                    raise RuntimeError('账户处于日亏停手；恢复接口不会清除风控停手')
                state['paused'] = False
                atomic_json(state_path,state)
                self.store.set(paused=False, last_error=None, failures=0)
                self.sync_latch()
                self.next_cycle = 0
                self.store.finish_command(command['id'],'completed','已恢复，下一轮按策略信号执行')
        except Exception as exc:
            self.store.set(paused=True,last_error=str(exc))
            self.sync_latch(cancel=False)
            self.store.finish_command(command['id'],'failed',str(exc))

    def run_cycle(self):
        slot_start = time.monotonic()
        identifier = self.store.start_cycle()
        stage = 'scan'
        try:
            self.ensure_ready()
            for stage in ('scan','execute','snapshot'):
                if self.shutdown.is_set() or self.store.get('paused',True):
                    self.store.cycle(identifier,state='canceled',ended=time.time(),message='控制请求已暂停本轮后续步骤')
                    return
                if stage == 'execute': self.authority.ensure()
                self.store.cycle(identifier,stage=stage)
                self.runner.run(stage)
            self.store.cycle(identifier,state='completed',ended=time.time())
            self.store.set(last_success=time.time(),last_error=None,failures=0)
            elapsed = time.monotonic()-slot_start
            self.next_cycle = slot_start + (int(elapsed//self.config.interval)+1)*self.config.interval
        except Exception as exc:
            canceled = self.store.get('paused',True) or self.shutdown.is_set()
            self.store.cycle(identifier,state='canceled' if canceled else 'failed',ended=time.time(),message='停止请求已取消扫描' if canceled else str(exc))
            if canceled: return
            failures = self.store.get('failures',0)+1
            self.store.set(failures=failures,last_error=str(exc))
            # Never blindly replay an ambiguous private order attempt.
            if stage != 'scan' or failures >= 3:
                self.store.set(paused=True); self.sync_latch()
            self.next_cycle = time.monotonic() + min(30 * 2 ** min(failures-1,4),300)

    def loop(self):
        while not self.shutdown.is_set():
            command = self.store.next_command()
            if command:
                self.handle(command)
                continue
            if self.config.enabled and not self.store.get('paused',True) and time.monotonic() >= self.next_cycle:
                self.run_cycle()
                continue
            self.wake.wait(1); self.wake.clear()

    def status(self):
        heartbeat = self.store.get('heartbeat')
        return dict(mode='okx_demo_only',enabled=self.config.enabled,paused=self.store.get('paused',True),
                    heartbeat_at=heartbeat,heartbeat_age_seconds=round(time.time()-heartbeat,1) if heartbeat else None,
                    worker_alive=self.store.get('worker_alive',False),last_success=self.store.get('last_success'),
                    last_error=self.store.get('last_error'),cycle=self.store.cycle(),command=self.store.command())

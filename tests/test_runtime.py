import json
import os
import sys
import tempfile
import time
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from runtime.config import Config,hash_password,verify_password
from runtime.engine import Engine,Authority,LEGACY_WORKFLOWS
from runtime.server import create_app
from runtime.state import Store
from runtime_guard import atomic_json,entries_allowed,check_scan
from okx_demo_trader import Mirror,new_state
from test_okx_demo_trader import FakeOKX,long_btc,paper,run

PASSWORD='test-only-password-123'
HASH=hash_password(PASSWORD)


class Allow:
    def ensure(self): pass


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=Config(self.root/'private',self.root/'data','https://panel.test',HASH,True)
        self.store=Store(self.config.private)
        (self.config.data/'combined').mkdir(parents=True)
        (self.config.data/'combined/state.json').write_text('{"positions":{}}')
        (self.config.data/'okx_demo').mkdir()
        atomic_json(self.config.data/'okx_demo/state.json',dict(new_state(),paused=False))
        atomic_json(self.config.data/'okx_demo/summary.json',dict(updated_at='2026-11-03T22:00:00Z',equity=5000,paused=False,positions=[]))
        self.calls=[]
        class FakeRunner:
            def run(inner,stage):
                self.calls.append(stage)
                if stage=='stop': atomic_json(self.config.data/'okx_demo/summary.json',dict(paused=True,stop_control={'status':'completed'}))
        self.engine=Engine(self.config,self.store,runner=FakeRunner(),authority=Allow(),ready=lambda:True)

    def tearDown(self): self.tmp.cleanup()

    def test_stop_is_durable_before_execution_and_deduplicates(self):
        self.store.set(paused=False);self.engine.sync_latch()
        one=self.engine.submit('stop','a'*20);two=self.engine.submit('stop','a'*20)
        self.assertEqual(one['id'],two['id']);self.assertTrue(self.store.get('paused'))
        self.assertTrue(json.loads(self.engine.latch.read_text())['paused'])
        self.assertEqual(self.calls,[])
        self.engine.handle(self.store.next_command())
        self.assertEqual(self.store.command(one['id'])['state'],'completed')
        self.assertEqual(self.calls,['stop'])
        self.engine.handle(dict(id='unused',action='resume'))
        self.assertFalse(self.store.get('paused'))
        self.engine.submit('stop','a'*20)
        self.assertFalse(json.loads(self.engine.latch.read_text())['paused'])

    def test_resume_cannot_overtake_an_accepted_stop(self):
        self.engine.submit('stop','a'*20)
        with self.assertRaises(ValueError):self.engine.submit('resume','b'*20)

    def test_private_failure_pauses_and_is_not_replayed(self):
        self.store.set(paused=False);self.engine.sync_latch()
        def fail(stage):
            self.calls.append(stage)
            if stage=='execute':raise RuntimeError('ambiguous private response')
        self.engine.runner.run=fail
        self.engine.run_cycle()
        self.assertEqual(self.calls,['scan','execute'])
        self.assertTrue(self.store.get('paused'))
        self.assertEqual(self.store.cycle()['state'],'failed')
        with patch.dict(os.environ,{'PANEL_RUNTIME_CONTROL_FILE':str(self.engine.latch)}):self.assertFalse(entries_allowed())

    def test_stop_during_scan_prevents_private_stage(self):
        self.store.set(paused=False);self.engine.sync_latch()
        def scan(stage):
            self.calls.append(stage);self.engine.submit('stop','c'*20)
        self.engine.runner.run=scan;self.engine.run_cycle()
        self.assertEqual(self.calls,['scan'])
        self.assertEqual(self.store.cycle()['state'],'canceled')

    def test_failed_scan_retries_three_times_then_pauses(self):
        self.store.set(paused=False);self.engine.sync_latch()
        self.engine.runner.run=lambda stage: (_ for _ in ()).throw(RuntimeError('scan failed'))
        self.engine.run_cycle();self.assertFalse(self.store.get('paused'))
        self.engine.run_cycle();self.engine.run_cycle();self.assertTrue(self.store.get('paused'))
        self.assertEqual(self.store.get('failures'),3)

    def test_restart_requeues_stop_and_pauses_interrupted_cycle(self):
        one=self.engine.submit('stop','d'*20);self.store.next_command();self.store.start_cycle()
        self.store.recover()
        self.assertEqual(self.store.command(one['id'])['state'],'queued')
        self.assertEqual(self.store.cycle()['state'],'interrupted')
        self.assertTrue(self.store.get('paused'))

    def test_interrupted_resume_does_not_silently_resume_after_restart(self):
        one=self.engine.submit('resume','r'*20);self.store.next_command();self.store.set(paused=False)
        self.store.recover()
        self.assertEqual(self.store.command(one['id'])['state'],'failed')
        self.assertTrue(self.store.get('paused'))

    def test_heartbeat_and_stop_acceptance_continue_during_long_scan(self):
        entered,release=threading.Event(),threading.Event()
        def stage(name):
            self.calls.append(name)
            if name=='scan':entered.set();release.wait(5)
            if name=='stop':atomic_json(self.config.data/'okx_demo/summary.json',dict(paused=True,stop_control={'status':'completed'}))
        self.engine.runner.run=stage;self.store.set(paused=False);self.engine.start()
        try:
            self.assertTrue(entered.wait(2))
            deadline=time.time()+2
            while not self.store.get('heartbeat') and time.time()<deadline:time.sleep(.01)
            self.assertTrue(self.store.get('heartbeat'))
            command=self.engine.submit('stop','s'*20)
            self.assertTrue(self.store.get('paused'))
            release.set();deadline=time.time()+2
            while self.store.command(command['id'])['state'] not in ('completed','failed') and time.time()<deadline:time.sleep(.01)
            self.assertEqual(self.store.command(command['id'])['state'],'completed')
            self.assertNotIn('execute',self.calls)
        finally:release.set();self.engine.stop()

    def test_exclusive_data_directory_lock(self):
        self.engine.start()
        other=Engine(self.config,self.store,runner=self.engine.runner,authority=Allow(),ready=lambda:True)
        try:
            with self.assertRaises(RuntimeError):other.start()
        finally:self.engine.stop()

    def test_stop_guard_blocks_entries_but_allows_closes(self):
        atomic_json(self.engine.latch,dict(paused=True,cancel_scan=True))
        fake=FakeOKX()
        with patch.dict(os.environ,{'PANEL_RUNTIME_CONTROL_FILE':str(self.engine.latch)}):
            run(fake,paper(**{'BTC-USDT-SWAP':long_btc()}))
            self.assertEqual(fake.posts,[])
            with self.assertRaises(RuntimeError):check_scan()
            self.engine.latch.unlink();self.assertFalse(entries_allowed())

    def test_database_latch_blocks_a_stale_open_file(self):
        atomic_json(self.engine.latch,dict(paused=False,cancel_scan=False));self.store.set(paused=True)
        with patch.dict(os.environ,{'PANEL_RUNTIME_CONTROL_FILE':str(self.engine.latch),'PANEL_RUNTIME_DB':str(self.store.path)}):self.assertFalse(entries_allowed())

    def test_current_daily_halt_is_not_cleared_by_resume(self):
        path=self.config.data/'okx_demo/state.json';value=json.loads(path.read_text())
        value.update(halted=True,day=datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d'));atomic_json(path,value)
        one=self.engine.submit('resume','e'*20);self.engine.handle(self.store.next_command())
        self.assertEqual(self.store.command(one['id'])['state'],'failed')
        self.assertTrue(json.loads(path.read_text())['halted'])

    def test_authority_requires_disabled_workflows_and_no_running_jobs(self):
        authority=Authority('owner/repo')
        workflows=[dict(id=i,path='.github/workflows/'+n,state='disabled_manually') for i,n in enumerate(LEGACY_WORKFLOWS)]
        with patch.object(authority,'read',side_effect=lambda suffix:{'workflows':workflows} if suffix.startswith('/actions/workflows?') else {'workflow_runs':[]}):authority.ensure()
        authority.checked=None;workflows[0]['state']='active'
        with patch.object(authority,'read',return_value={'workflows':workflows}):
            with self.assertRaises(RuntimeError):authority.ensure()
        workflows[0]['state']='disabled_manually';authority.checked=None
        with patch.object(authority,'read',side_effect=lambda suffix:{'workflows':workflows} if suffix.startswith('/actions/workflows?') else {'workflow_runs':[{'workflow_id':0}]}):
            with self.assertRaises(RuntimeError):authority.ensure()

    def test_http_login_csrf_idempotency_and_private_file_boundary(self):
        def factory(config,store):return Engine(config,store,runner=self.engine.runner,authority=Allow(),ready=lambda:True)
        app=create_app(self.config,engine_factory=factory)
        origin={'Origin':self.config.origin}
        with TestClient(app,base_url=self.config.origin) as c:
            self.assertEqual(c.get('/api/status').status_code,401)
            self.assertEqual(c.post('/api/control/stop',json={},headers=origin).status_code,401)
            self.assertEqual(c.get('/.env').status_code,404)
            self.assertEqual(c.get('/runtime/server.py').status_code,404)
            self.assertEqual(c.get('/api/public/health').json()['mode'],'okx_demo_only')
            self.assertEqual(c.post('/api/login',json={'username':'admin','password':PASSWORD}).status_code,403)
            r=c.post('/api/login',json={'username':'admin','password':PASSWORD},headers=origin)
            self.assertEqual(r.status_code,200);csrf=r.json()['csrf']
            cookie=r.headers['set-cookie'];self.assertIn('HttpOnly',cookie);self.assertIn('Secure',cookie);self.assertIn('SameSite=strict',cookie)
            self.assertEqual(c.post('/api/control/stop',json={},headers=origin).status_code,403)
            headers={**origin,'X-CSRF-Token':csrf,'Idempotency-Key':'test-stop-command-0001'}
            r=c.post('/api/control/stop',json={},headers=headers);self.assertEqual(r.status_code,202)
            duplicate=c.post('/api/control/stop',json={},headers=headers)
            self.assertEqual(r.json()['id'],duplicate.json()['id'])
            self.assertEqual(c.get('/api/commands/'+r.json()['id']).status_code,200)
            self.assertNotIn('PANEL_PASSWORD_HASH',c.get('/runtime-config.json').text)
            self.assertEqual(c.post('/api/logout',json={},headers=headers).status_code,200)
            self.assertEqual(c.get('/api/status').status_code,401)

    def test_login_rate_limit_and_password_hash_verification(self):
        self.assertTrue(verify_password(PASSWORD,HASH));self.assertFalse(verify_password('wrong',HASH))
        disabled=Config(self.config.private,self.config.data,self.config.origin,HASH,False)
        with TestClient(create_app(disabled),base_url=self.config.origin) as c:
            for _ in range(5):self.assertEqual(c.post('/api/login',json={'username':'admin','password':'wrong'},headers={'Origin':self.config.origin}).status_code,401)
            self.assertEqual(c.post('/api/login',json={'username':'admin','password':'wrong'},headers={'Origin':self.config.origin}).status_code,429)


if __name__=='__main__':unittest.main()

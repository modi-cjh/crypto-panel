import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, directory):
        directory = Path(directory); directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / 'runtime.sqlite3'
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, idem TEXT UNIQUE, action TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, message TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS logins (ip TEXT NOT NULL, at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS cycles (id INTEGER PRIMARY KEY, state TEXT NOT NULL, stage TEXT NOT NULL, started REAL NOT NULL, ended REAL, message TEXT NOT NULL DEFAULT '');
            ''')
            db.execute("INSERT OR IGNORE INTO meta VALUES ('paused','true')")
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, **values):
        with self.db() as db:
            db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', [(k,json.dumps(v)) for k,v in values.items()])

    def enqueue(self, action, idem):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM commands WHERE idem=?', (idem,)).fetchone()
            if existing:
                if existing['action'] != action: raise ValueError('Idempotency key belongs to another action')
                return dict(existing)
            running = db.execute("SELECT * FROM commands WHERE state IN ('queued','running') ORDER BY created LIMIT 1").fetchone()
            if running:
                if running['action'] != action: raise ValueError('Another control request is still being processed')
                return dict(running)
            if action == 'stop':
                db.execute("INSERT OR REPLACE INTO meta VALUES ('paused','true')")
            identifier = secrets.token_hex(16)
            db.execute('INSERT INTO commands(id,idem,action,state,created,updated) VALUES (?,?,?,?,?,?)', (identifier,idem,action,'queued',now,now))
            return dict(db.execute('SELECT * FROM commands WHERE id=?', (identifier,)).fetchone())

    def command(self, identifier=None):
        with self.db() as db:
            row = db.execute('SELECT id,action,state,created,updated,message FROM commands WHERE id=?', (identifier,)).fetchone() if identifier else db.execute('SELECT id,action,state,created,updated,message FROM commands ORDER BY created DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def next_command(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM commands WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not row: return None
            db.execute("UPDATE commands SET state='running',updated=? WHERE id=?", (time.time(),row[0]))
        return self.command(row[0])

    def finish_command(self, identifier, state, message=''):
        with self.db() as db:
            db.execute('UPDATE commands SET state=?,message=?,updated=? WHERE id=?', (state,message,time.time(),identifier))

    def recover(self):
        with self.db() as db:
            interrupted = db.execute("SELECT COUNT(*) FROM cycles WHERE state='running'").fetchone()[0]
            interrupted_resume = db.execute("SELECT COUNT(*) FROM commands WHERE state='running' AND action='resume'").fetchone()[0]
            if interrupted_resume:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('paused','true')")
            if interrupted:
                db.execute("UPDATE cycles SET state='interrupted',ended=?,message='重启前任务中断；需核对账户后恢复' WHERE state='running'", (time.time(),))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('paused','true')")
            db.execute("UPDATE commands SET state='failed',updated=?,message='恢复请求在重启前中断，请核对账户后重新确认' WHERE state='running' AND action='resume'", (time.time(),))
            db.execute("UPDATE commands SET state='queued',updated=? WHERE state='running' AND action='stop'", (time.time(),))
            pending_stop = db.execute("SELECT COUNT(*) FROM commands WHERE action='stop' AND state='queued'").fetchone()[0]
            if pending_stop: db.execute("INSERT OR REPLACE INTO meta VALUES ('paused','true')")

    def start_cycle(self):
        with self.db() as db:
            return db.execute("INSERT INTO cycles(state,stage,started) VALUES ('running','scan',?)", (time.time(),)).lastrowid

    def cycle(self, identifier=None, **values):
        with self.db() as db:
            if values:
                allowed = {k:v for k,v in values.items() if k in ('state','stage','ended','message')}
                db.execute('UPDATE cycles SET ' + ','.join(k+'=?' for k in allowed) + ' WHERE id=?', [*allowed.values(),identifier])
            row = db.execute('SELECT * FROM cycles WHERE id=?', (identifier,)).fetchone() if identifier else db.execute('SELECT * FROM cycles ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def login_allowed(self, ip):
        now = time.time()
        with self.db() as db:
            db.execute('DELETE FROM logins WHERE at<?', (now-300,))
            count = db.execute('SELECT COUNT(*) FROM logins WHERE ip=?', (ip,)).fetchone()[0]
            if count >= 5: return False
            db.execute('INSERT INTO logins VALUES (?,?)', (ip,now))
        return True

    def create_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(),csrf,time.time()+28800))
        return token, csrf

    def session(self, token):
        if not token: return None
        with self.db() as db:
            row = db.execute('SELECT csrf FROM sessions WHERE token=? AND expires>?', (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        return row[0] if row else None

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256((token or '').encode()).hexdigest(),))

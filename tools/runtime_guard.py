"""Local runtime stop latch. Unconfigured GitHub jobs retain their existing behavior."""
import json
import os
import tempfile
import sqlite3
from pathlib import Path


def atomic_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def latch():
    path = os.environ.get('PANEL_RUNTIME_CONTROL_FILE')
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value.get('paused'), bool): raise ValueError('invalid latch')
        return value
    except (OSError, ValueError, TypeError):
        return dict(paused=True, cancel_scan=True)


def entries_allowed():
    state = latch()
    if state is not None and state['paused'] is not False:
        return False
    database = os.environ.get('PANEL_RUNTIME_DB')
    if database:
        try:
            with sqlite3.connect('file:'+database+'?mode=ro',uri=True,timeout=1) as db:
                row = db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()
            return bool(row and json.loads(row[0]) is False)
        except (sqlite3.Error,OSError,ValueError):
            return False
    return True


def check_scan():
    state = latch()
    if state and state.get('cancel_scan'):
        raise RuntimeError('Scan canceled by the runtime stop request')

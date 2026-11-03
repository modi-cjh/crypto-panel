import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def hash_password(password):
    if len(password) < 12:
        raise ValueError('Admin password must contain at least 12 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000)
    return 'pbkdf2_sha256$600000$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(digest).decode()


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 100000 <= int(rounds) <= 1000000:
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Config:
    private: Path
    data: Path
    origin: str
    password_hash: str
    enabled: bool = False
    local_http: bool = False
    interval: int = 300
    repository: str = 'modi-cjh/crypto-panel'

    @classmethod
    def from_env(cls):
        local = os.environ.get('PANEL_LOCAL_HTTP') == '1'
        origin = os.environ.get('PANEL_PUBLIC_ORIGIN', '').rstrip('/')
        u = urlsplit(origin)
        if u.username or u.password or u.path or u.query or u.fragment or not u.netloc:
            raise ValueError('Set PANEL_PUBLIC_ORIGIN to the exact HTTPS origin')
        if u.scheme != 'https' and not (local and u.scheme == 'http' and u.hostname in ('127.0.0.1', 'localhost')):
            raise ValueError('HTTPS is required; local HTTP is allowed only on loopback')
        encoded = os.environ.get('PANEL_PASSWORD_HASH', '')
        if not encoded.startswith('pbkdf2_sha256$'):
            raise ValueError('Configure PANEL_PASSWORD_HASH using python -m runtime.admin password')
        private = Path(os.environ.get('PANEL_PRIVATE_DIR', '/var/lib/crypto-panel/private')).resolve()
        data = Path(os.environ.get('PANEL_DATA_DIR', '/var/lib/crypto-panel/data')).resolve()
        for p in (private, data):
            if p == ROOT or ROOT in p.parents:
                raise ValueError('Runtime state and secrets must be outside the public source checkout')
        interval = int(os.environ.get('PANEL_CYCLE_SECONDS', '300'))
        if not 60 <= interval <= 3600:
            raise ValueError('Cycle interval must be 60..3600 seconds')
        return cls(private, data, origin, encoded, os.environ.get('PANEL_EXECUTION_ENABLED') == '1', local, interval)


def credentials_ready():
    return all(os.environ.get(k, '').strip() for k in ('OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE'))

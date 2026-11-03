import hmac
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Config, ROOT, verify_password
from .engine import Engine
from .state import Store


def create_app(config=None, engine_factory=Engine):
    config = config or Config.from_env()
    store = Store(config.private)
    engine = engine_factory(config, store)

    @asynccontextmanager
    async def lifespan(app):
        engine.start()
        try: yield
        finally: engine.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine, app.state.store = engine, store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(config.origin).hostname])

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        if not config.local_http:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    async def body(request):
        if request.headers.get('origin') != config.origin:
            raise HTTPException(403, 'Origin verification failed')
        if request.headers.get('content-type','').split(';')[0] != 'application/json':
            raise HTTPException(415, 'JSON requests are required')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192: raise HTTPException(413, 'Request too large')
        try:
            value = json.loads(raw)
            if not isinstance(value,dict): raise ValueError()
            return value
        except (ValueError, UnicodeDecodeError): raise HTTPException(400,'Invalid JSON')

    def session(request, mutation=False):
        csrf = store.session(request.cookies.get('panel_session'))
        if not csrf: raise HTTPException(401,'请先登录控制台')
        if mutation and not hmac.compare_digest(request.headers.get('x-csrf-token','').encode(),csrf.encode()):
            raise HTTPException(403,'CSRF verification failed')
        return csrf

    @app.get('/api/session')
    def current_session(request: Request):
        return dict(user='admin',csrf=session(request))

    @app.post('/api/login')
    async def login(request: Request):
        data = await body(request)
        if not store.login_allowed(request.client.host):
            raise HTTPException(429,'登录尝试过多，请五分钟后重试',headers={'Retry-After':'300'})
        password, user = data.get('password'), data.get('username')
        if not isinstance(password,str) or user!='admin' or len(password)>1024 or not verify_password(password,config.password_hash):
            raise HTTPException(401,'账号或密码错误')
        old = request.cookies.get('panel_session')
        if old: store.logout(old)
        token, csrf = store.create_session()
        response = JSONResponse(dict(user='admin',csrf=csrf))
        response.set_cookie('panel_session',token,max_age=28800,httponly=True,secure=not config.local_http,samesite='strict',path='/')
        return response

    @app.post('/api/logout')
    async def logout(request: Request):
        await body(request); session(request,mutation=True)
        store.logout(request.cookies.get('panel_session'))
        response = JSONResponse(dict(ok=True)); response.delete_cookie('panel_session',path='/')
        return response

    @app.get('/api/status')
    def status(request: Request):
        session(request)
        return engine.status()

    @app.post('/api/control/{action}', status_code=202)
    async def control(action: str, request: Request):
        await body(request); session(request,mutation=True)
        idem = request.headers.get('idempotency-key','')
        if not 16 <= len(idem) <= 80 or not all(c.isalnum() or c in '-_' for c in idem):
            raise HTTPException(400,'A valid Idempotency-Key is required')
        try: return engine.submit(action,idem)
        except ValueError as exc: raise HTTPException(409,str(exc))

    @app.get('/api/commands/{identifier}')
    def command(identifier: str, request: Request):
        session(request)
        result = store.command(identifier)
        if not result: raise HTTPException(404,'Command not found')
        return result

    @app.get('/api/public/health')
    def health():
        value = engine.status()
        return {k:value[k] for k in ('mode','enabled','paused','heartbeat_at','heartbeat_age_seconds','worker_alive','last_success')}

    @app.get('/api/public/account')
    def account():
        try:
            data = json.loads((config.data/'okx_demo/summary.json').read_text())
            state = json.loads((config.data/'okx_demo/state.json').read_text())
        except (OSError, ValueError): raise HTTPException(503,'账户数据尚未导入或无法读取')
        data['paused'] = store.get('paused',True) or state.get('paused',True)
        data['execution_runtime'] = 'persistent'
        last = store.command()
        if last and last['action']=='stop':
            data['stop_control'] = dict(status={'queued':'pending','running':'pending','completed':'completed','failed':'failed'}[last['state']],requested_at=last['created'])
        return data

    @app.get('/runtime-config.json')
    def runtime_config():
        return dict(mode='persistent',control_url=config.origin+'/control',account_url=config.origin+'/api/public/account',health_url=config.origin+'/api/public/health')

    @app.get('/control')
    def console(): return FileResponse(ROOT/'runtime/console.html')

    @app.get('/')
    def index(): return FileResponse(ROOT/'index.html')

    @app.get('/config.js')
    def public_config(): return FileResponse(ROOT/'config.js',media_type='text/javascript')

    app.mount('/assets',StaticFiles(directory=ROOT/'assets'),name='assets')
    app.mount('/paper',StaticFiles(directory=ROOT/'paper'),name='legacy-paper')
    return app


if __name__ == '__main__':
    import uvicorn
    # Reverse-proxy TLS and a single process; the data-directory lock rejects a second executor.
    uvicorn.run(create_app(),host='127.0.0.1',port=8011,proxy_headers=True,forwarded_allow_ips='127.0.0.1')

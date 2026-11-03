#!/usr/bin/env python3
"""Read-only diagnosis of demo quotes and the browser's CORS preflight. No API key or orders."""
import asyncio
import json
import urllib.request

ORIGIN='https://modi-cjh.github.io'
REST='https://www.okx.com/api/v5/public/mark-price?instType=SWAP&instId=XRP-USDT-SWAP'


def rest(method):
    headers={'Origin':ORIGIN,'User-Agent':'crypto-panel-quote-diagnostic'}
    if method=='OPTIONS':
        headers.update({'Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'x-simulated-trading'})
    else:headers['x-simulated-trading']='1'
    try:
        request=urllib.request.Request(REST,headers=headers,method=method)
        with urllib.request.urlopen(request,timeout=8) as response:
            body=response.read(262144).decode()
            data=json.loads(body) if method=='GET' else {}
            return dict(method=method,http=response.status,allow_origin=response.headers.get('Access-Control-Allow-Origin'),
                        allow_headers=response.headers.get('Access-Control-Allow-Headers'),
                        code=data.get('code'),sample=(data.get('data') or [])[:1])
    except Exception as exc:return dict(method=method,error=str(exc))


async def ws(url):
    from websockets.asyncio.client import connect
    try:
        async with asyncio.timeout(10):
            async with connect(url,origin=ORIGIN,open_timeout=5,close_timeout=1) as connection:
                await connection.send(json.dumps(dict(op='subscribe',args=[dict(channel='mark-price',instId='XRP-USDT-SWAP')])))
                async for raw in connection:
                    data=json.loads(raw)
                    if data.get('event')=='error':return dict(url=url,result=data)
                    if data.get('data'):
                        return dict(url=url,result='valid_demo_mark',sample=data['data'][:1])
    except Exception as exc:return dict(url=url,error=type(exc).__name__+': '+str(exc))


async def main():
    results=await asyncio.gather(ws('wss://wspap.okx.com:8443/ws/v5/public'),ws('wss://wspap.okx.com/ws/v5/public'),asyncio.to_thread(rest,'GET'),asyncio.to_thread(rest,'OPTIONS'))
    print('QUOTE_DIAGNOSTIC_BEGIN')
    print(json.dumps(results,ensure_ascii=False,indent=2))
    print('QUOTE_DIAGNOSTIC_END')


if __name__=='__main__':asyncio.run(main())

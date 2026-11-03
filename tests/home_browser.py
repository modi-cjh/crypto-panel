"""Home contracts and mobile artifacts. Synthetic positions never leave request interception."""
import copy
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
BASE=os.environ.get('PANEL_URL','http://127.0.0.1:8008/')
FIXTURE=json.loads((ROOT/'tests/fixtures/home.json').read_text())

def account_fixture():
    now=datetime.now(timezone.utc)
    stamp=lambda hours:(now-timedelta(hours=hours)).isoformat()
    data=json.loads((ROOT/'paper/account.json').read_text())
    data.pop('pause',None);data['updated_at']=stamp(0)
    a=data['accounts'][0]
    a.update(status='运行中',updated_at=stamp(0),balance=100,equity=101.4,daily_pnl=1.4,
             day=(now+timedelta(hours=7)).date().isoformat(),day_start=100,daily_loss_limit=.08,
             positions=copy.deepcopy(FIXTURE['positions']),strategy_enabled=True,
             strategies={'mean_reversion':{'rsi':25,'trend':.02}},enabled_strategies=['mean_reversion'])
    a['equity_curve']=[dict(timestamp=stamp(p['hours']),equity=p['value']) for p in FIXTURE['equity']]
    return data

def setup(context,data=None):
    errors=[];context.on('page',lambda page:page.on('pageerror',lambda e:errors.append(str(e))))
    market=json.loads((ROOT/'tests/fixtures/market.json').read_text())
    context.route_web_socket('**/ws/v5/**',lambda ws:ws.on_message(lambda m:ws.send('pong') if m=='ping' else None))
    def api(route):
        path=urlparse(route.request.url)
        if path.path.endswith('/candles'):
            result=copy.deepcopy(market['candles']);symbol=parse_qs(path.query)['instId'][0]
            anchor=68000 if symbol.startswith('BTC-') else 100
            for i,row in enumerate(result['data']):
                row[0]=str(int(time.time()//3600*3600000)-i*3600000)
                for j,offset in ((1,0),(2,.02),(3,-.02),(4,.01)):
                    row[j]=str(anchor*(1+offset+(i%5)*.001))
        elif path.path.endswith('/tickers'):
            result={'code':'0','data':[dict(instId=p['symbol'],last=str(p['mark_price']),open24h=str(p['entry']),volCcy24h='100000') for p in data['accounts'][0]['positions']]} if data and data['accounts'][0]['positions'] else market['tickers']
        else:result=market['funding'] if path.path.endswith('/funding-rate') else market['interest']
        route.fulfill(json=result)
    context.route('https://www.okx.com/api/v5/**',api)
    if data:
        context.route('**/paper/account.json?*',lambda r:r.fulfill(json=data))
        context.route('**/paper/scan.json?*',lambda r:r.fulfill(json=dict(schema_version=1,generated_at=data['updated_at'],source_status='ok',positions=[],recent_signals=[],instruments=[dict(instId=p['symbol'],quote_24h=1000000,spread=.001,leverage_cap=5,signals={},held=True) for p in data['accounts'][0]['positions']])))
    return errors

def no_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')

def contracts(browser):
    data=account_fixture();context=browser.new_context(viewport={'width':390,'height':844},has_touch=True,is_mobile=True,reduced_motion='reduce')
    errors=setup(context,data);page=context.new_page();page.goto(BASE,wait_until='networkidle')
    page.wait_for_selector('.position-row');assert page.locator('.position-row').count()==2
    assert page.locator('#total-equity').inner_text()=='101.40'
    assert '+1.40 USDT / +1.40%' in page.locator('#total-pnl').inner_text()
    paths=[]
    for period,count in [('day',3),('week',5),('all',6)]:
        page.locator(f'#home-equity-range [data-range="{period}"]').click()
        assert page.locator('#equity-chart svg').get_attribute('data-points')==str(count)
        assert page.locator(f'#home-equity-range [data-range="{period}"]').get_attribute('aria-pressed')=='true'
        paths.append(page.locator('#equity-chart path').get_attribute('d'))
    assert len(set(paths))==3
    assert page.locator('#coin-tabs,#chart-symbol,.room').count()==0
    assert page.locator('.position-detail:visible').count()==0
    first=page.locator('.position-row').nth(0);second=page.locator('.position-row').nth(1)
    assert first.locator('.coin-icon img').get_attribute('loading')=='lazy'
    assert second.locator('.coin-icon img').count()==0
    assert second.locator('.coin-icon').inner_text()=='S'
    first.click();page.wait_for_selector('.position-detail:not([hidden]) #price-chart canvas')
    assert first.get_attribute('aria-expanded')=='true'
    assert page.locator('.position-detail:visible').count()==1
    assert '保证金 27.20 USDT · 杠杆 5×' in page.locator('.position-detail:visible').inner_text()
    assert all(text in page.locator('#chart-levels').inner_text() for text in ['入场 68,000','止损 65,000','止盈 74,000'])
    assert page.locator('#price-panel').count()==1
    page.evaluate('window.homeCandlePanel=document.querySelector("#price-panel")')
    second.click();page.wait_for_function("() => document.querySelector('#chart-levels').textContent.includes('入场 100')")
    assert page.evaluate('window.homeCandlePanel===document.querySelector("#price-panel")')
    assert first.get_attribute('aria-expanded')=='false';assert second.get_attribute('aria-expanded')=='true'
    assert page.locator('.position-detail:visible').count()==1
    page.locator('.position-detail:visible [data-timeframe="4H"]').click()
    page.wait_for_function("() => document.querySelector('#analysis-period').textContent.includes('4 小时')")
    second.click();assert page.locator('.position-detail:visible').count()==0
    second.focus();page.keyboard.press('Enter');assert second.get_attribute('aria-expanded')=='true'
    # Refreshing quote/account data must preserve the expanded row and candle node.
    data['accounts'][0]['positions'][1]['mark_price']=99
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("() => document.querySelectorAll('.position-row')[1].textContent.includes('+0.50')")
    assert second.get_attribute('aria-expanded')=='true'
    assert page.evaluate('window.homeCandlePanel===document.querySelector("#price-panel")')
    # Broken local images expose initials, and no icon uses an external host.
    first.locator('img').evaluate("img=>img.dispatchEvent(new Event('error'))")
    assert first.locator('img').count()==0;assert first.locator('.coin-icon').inner_text()=='B'
    assert page.locator('#scan-body .coin-icon').count()==2
    for width in (320,360,390,1440):
        page.set_viewport_size({'width':width,'height':844});no_overflow(page)
    # Empty positions are independent of pause: only the PAUSED-derived flag adds the pause message.
    data['accounts'][0]['positions']=[]
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("() => document.querySelector('#live-positions').textContent==='暂无持仓'")
    assert not page.locator('.position-detail:visible').count()
    assert not errors,errors;context.close()
    print('Home browser: equity card/ranges, accordion/keyboard, reused chart/levels/timeframe, refreshed quotes, icon fallback, 320–1440px passed.')

def artifacts(browser):
    real=browser.new_context(viewport={'width':390,'height':844},device_scale_factor=1,reduced_motion='reduce')
    errors=setup(real);page=real.new_page();page.goto(BASE,wait_until='networkidle');page.evaluate('document.fonts.ready')
    page.wait_for_function("() => document.querySelector('#live-positions').textContent.includes('模拟盘已暂停，暂无持仓')")
    assert page.locator('#total-equity').inner_text()=='100.00'
    assert page.locator('#equity-chart svg').get_attribute('data-points')=='0'
    assert page.locator('#equity-error').inner_text()=='暂无记录'
    page.screenshot(path=str(ROOT/'shot-home.png'),animations='disabled')
    assert not errors,errors;real.close()
    # Two positions come exclusively from tests/fixtures/home.json via browser routes.
    with tempfile.TemporaryDirectory(prefix='panel-home-video-') as temp:
        context=browser.new_context(viewport={'width':390,'height':844},device_scale_factor=1,has_touch=True,is_mobile=True,
                                    record_video_dir=temp,record_video_size={'width':390,'height':844})
        errors=setup(context,account_fixture());page=context.new_page();page.goto(BASE,wait_until='networkidle')
        page.wait_for_selector('.position-row');page.evaluate('document.fonts.ready')
        first=page.locator('.position-row').first;first.tap();page.wait_for_selector('.position-detail:not([hidden]) #price-chart canvas')
        page.locator('.execution-panel').evaluate('e=>window.scrollTo(0,e.offsetTop-155)')
        page.wait_for_timeout(250);page.screenshot(path=str(ROOT/'shot-home-demo.png'),animations='disabled')
        first.tap();page.evaluate('window.scrollTo(0,0)');page.wait_for_timeout(300)
        start=time.monotonic()
        for period in ('day','week','all'):
            page.locator(f'#home-equity-range [data-range="{period}"]').tap();page.wait_for_timeout(350)
        first.tap();page.wait_for_timeout(600)
        page.evaluate('window.scrollTo(0,0)');page.locator('#mascot-touch').tap();page.wait_for_selector('#mascot-drawer[open]')
        page.locator('#companion-tab-team').tap();page.wait_for_timeout(1000)
        elapsed=time.monotonic()-start
        assert elapsed<9,elapsed
        source=page.video.path();context.close()
        # Trim the setup portion while retaining the full interaction sequence.
        total=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',source]))
        subprocess.run(['ffmpeg','-v','error','-y','-ss',str(max(0,total-elapsed-.3)),'-i',source,'-t',str(min(9,elapsed+.3)),
                        '-an','-c:v','libvpx-vp9','-deadline','realtime','-cpu-used','5','-crf','30','-b:v','0',str(ROOT/'rec-home.webm')],check=True)
        duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(ROOT/'rec-home.webm')]))
        assert 0<duration<=10,duration
        assert not errors,errors
        print(f'Artifacts: shot-home.png (real paused), shot-home-demo.png (2 fixture positions), rec-home.webm {duration:.2f}s; 390×844; pageerror=0.')

def main():
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        try:contracts(browser);artifacts(browser)
        finally:browser.close()
if __name__=='__main__':main()

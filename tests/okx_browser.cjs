// OKX demo-trading account acceptance: PANEL_URL=http://127.0.0.1:8008/ node tests/okx_browser.cjs
// The paper-data feed is served through route interception only; OKX public endpoints are blocked.
const { chromium, devices } = require('playwright');
const assert = require('node:assert/strict');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const iso = ms => new Date(ms).toISOString();
const now = Date.now();
let feed = {
  updated_at: iso(now - 600e3), equity: 5012.5, available: 4900, day_start: 5000, start_equity: 5000, paused: false, halted: false, dry_run: false, closed_trades: 1,
  history: [[now - 7200e3, 5000], [now - 3600e3, 4995], [now - 600e3, 5012.5]],
  positions: [{ inst: 'BTC-USDT-SWAP', side: 'long', contracts: '3', avgPx: '70000', markPx: '70400', upl: '12.5', uplRatio: '0.05', lever: '5', margin: '420', ctVal: 0.01, ours: true }],
  open_mirrors: [{ inst: 'BTC-USDT-SWAP', strategy: 'breakout_long', stop: 69000, status: 'open' }],
  recent_actions: [
    { ts: iso(now - 600e3), action: 'open', inst: 'BTC-USDT-SWAP', strategy: 'breakout_long', side: 1, contracts: 3, price: 70000, stop: 69000, leverage: 5, dry_run: false },
    { ts: iso(now - 4000e3), action: 'skip', inst: 'XRP-USDT-SWAP', reason: 'paper_entry_too_old', dry_run: false }],
};
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ ...devices['iPhone 13'] });
  let mode = 'ok', paperRequests = 0;
  await context.route(/okx\.com/, r => r.abort());
  // OKX demo public WebSocket: demo-market mark prices for the demo positions (pushed once the test asks for them).
  const demoSockets = [], demoSubs = [];
  await context.routeWebSocket(/wspap\.okx\.com/, ws => { demoSockets.push(ws); ws.onMessage(m => { if (m === 'ping') return ws.send('pong'); const b = JSON.parse(m); if (b.op === 'subscribe') demoSubs.push(...b.args); }); });
  await context.routeWebSocket(/ws\.okx\.com/, ws => ws.close());
  const pushMark = (instId, markPx) => demoSockets.at(-1).send(JSON.stringify({ arg: { channel: 'mark-price', instId }, data: [{ instId, markPx: String(markPx), ts: String(Date.now()) }] }));
  await context.route('**/paper-data/dashboard.json*', r => { paperRequests++; r.abort(); });
  await context.route('**/paper-data/okx_demo/summary.json*', r => mode === 'ok' ? r.fulfill({ body: JSON.stringify(feed), contentType: 'application/json' }) : r.fulfill({ status: 404, body: 'missing' }));
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(BASE);
  // The original bot snapshot is paused, so the OKX demo account leads the home page.
  await page.waitForSelector('#home-sim:not([hidden])');
  assert.ok(await page.evaluate(() => document.documentElement.classList.contains('sim-primary')));
  assert.equal(await page.locator('#pause-banner').isVisible(), false);
  assert.equal(await page.locator('#home-page > .execution-panel').isVisible(), false);
  assert.match(await page.locator('#pinned-label-text').innerText(), /OKX 模拟权益/);
  assert.match(await page.locator('#pinned-equity').innerText(), /^5,012\.50$/);
  assert.match(await page.locator('#okx-total-ret').innerText(), /累计收益 \+0\.25%.*今日 \+12\.50 USDT/);
  assert.match(await page.locator('#home-okx-status').innerText(), /运行中/);
  assert.equal(await page.locator('#home-okx-curve polyline').count(), 1);
  assert.match(await page.locator('#home-okx-positions').innerText(), /趋势突破只做多/);
  assert.match(await page.locator('#home-okx-actions').innerText(), /信号已超过 2 小时/);
  assert.equal(paperRequests, 0, 'the paper account feed is no longer read');
  // With prices unchanged (OKX is blocked here) the equity numbers must not animate on each refresh.
  await page.evaluate(() => { window.__rolls = 0; for (const id of ['pinned-equity', 'okx-total-equity'])
    new MutationObserver(m => { window.__rolls += m.filter(r => [...r.addedNodes].some(n => n.classList?.contains('number-in'))).length; }).observe(document.getElementById(id), { childList: true }); });
  await page.waitForTimeout(20000);
  assert.equal(await page.evaluate(() => window.__rolls), 0, 'equity animated without a change');
  // Floating PnL follows the demo market's own mark price (not the live exchange): 3 contracts × 0.01 BTC × (70,500 − 70,000).
  // (the 20 s quiet period above lets the stream's no-data watchdog reconnect, so there can be more than one subscribe)
  assert.ok(demoSubs.length >= 1 && demoSubs.every(x => x.channel === 'mark-price' && x.instId === 'BTC-USDT-SWAP'));
  pushMark('BTC-USDT-SWAP', 70500);
  await page.waitForFunction(() => document.querySelector('#home-okx-positions').textContent.includes('+15.00 USDT'));
  assert.match(await page.locator('#home-okx-positions').innerText(), /标记价（实时）\s*70,500/);
  await page.waitForFunction(() => document.querySelector('#pinned-equity').textContent.trim() === '5,015.00');   // 5,012.50 − 12.50 + 15.00
  await page.locator('#legacy-toggle').click();
  assert.equal(await page.locator('#home-page > .execution-panel').isVisible(), true);   // original bot still reachable
  await page.locator('#legacy-toggle').click();
  await page.locator('[data-nav="sim"]').click();
  await page.waitForSelector('#sim-page:not([hidden]) #okx-metrics .history-metrics');
  assert.equal(await page.locator('[data-nav="sim"]').getAttribute('aria-current'), 'page');
  const metrics = await page.locator('#okx-metrics').innerText();
  assert.match(metrics, /5,01(5\.00|2\.50)/);   // live demo mark while fresh, else the recorded equity
  assert.match(metrics, /已平仓\s*1 笔/);
  assert.equal(await page.locator('#okx-curve polyline').count(), 1);
  assert.match(await page.locator('#okx-start').innerText(), /起始 5,000\.00/);
  assert.equal(await page.locator('#okx-positions .sim-position').count(), 1);
  assert.equal(await page.locator('#okx-actions li').count(), 2);
  for (const width of [320, 360, 390]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow at ${width}`);
  }
  // The companion follows the OKX account and announces a new exchange action.
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.state === 'excited');   // just opened BTC
  feed = { ...feed, updated_at: iso(Date.now()), positions: [], open_mirrors: [], closed_trades: 2,
    recent_actions: [{ ts: iso(Date.now()), action: 'closed_on_exchange', inst: 'BTC-USDT-SWAP', dry_run: false }, ...feed.recent_actions] };
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.state === 'sad');
  await page.waitForFunction(() => document.querySelector('#toasts').textContent.includes('OKX 模拟 · BTC 交易所止损 / 止盈触发'));
  assert.match(await page.locator('#okx-positions').innerText(), /当前没有持仓/);
  await page.locator('#mascot-touch').click(); await page.waitForSelector('#mascot-drawer[open]');
  assert.match(await page.locator('#companion-panel-status').innerText(), /账户权益/);
  assert.match(await page.locator('.companion-accounts').innerText(), /OKX 模拟账户/);
  await page.keyboard.press('Escape');
  mode = 'missing'; await page.reload(); await page.waitForSelector('#sim-error:not([hidden])');
  assert.match(await page.locator('#sim-error').innerText(), /读取失败/);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('OKX 模拟账户验收通过：首页以 OKX 模拟账户为主（顶栏权益、累计 / 今日收益、资金曲线、持仓、交易动作、可展开原主力盘）、不再读取模拟盘账户、价格不变时不滚动、完整页面指标与曲线、320/360/390 无溢出、伙伴跟随并播报交易所止损、读取失败提示、pageerror=0。');
})().catch(e => { console.error(e); process.exit(1); });

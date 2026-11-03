// Demo mark-price REST fallback: PANEL_URL=http://127.0.0.1:8008/ node tests/okx_rest_browser.cjs
// Every WebSocket fails (as in some Safari setups); demo mark prices then come from REST with the demo header.
const { chromium, devices } = require('playwright');
const assert = require('node:assert/strict');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const iso = ms => new Date(ms).toISOString(), now = Date.now();
const feed = { updated_at: iso(now - 600e3), equity: 4980.83, available: 4800, day_start: 5000, start_equity: 5000, paused: false, halted: false, closed_trades: 0,
  history: [[now - 3600e3, 5000], [now - 600e3, 4980.83]],
  positions: [{ inst: 'XRP-USDT-SWAP', side: 'short', contracts: '15.8', avgPx: '1.2595', markPx: '1.271', upl: '-18.17', lever: '10', margin: '199', ctVal: 100, ours: true }],
  open_mirrors: [{ inst: 'XRP-USDT-SWAP', strategy: 'mean_reversion', stop: 1.2918, status: 'open' }], recent_actions: [] };
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ ...devices['iPhone 13'] });
  const demoHeaders = [];
  await context.routeWebSocket(/okx\.com/, ws => ws.close({ code: 1006 }));
  await context.route('**/paper-data/okx_demo/summary.json*', r => r.fulfill({ body: JSON.stringify(feed), contentType: 'application/json' }));
  await context.route('https://www.okx.com/api/v5/**', r => {
    const url = new URL(r.request().url()), demo = r.request().headers()['x-simulated-trading'];
    if (url.pathname.endsWith('/public/mark-price')) {
      demoHeaders.push(demo);
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: '0', data: demo === '1' ? [{ instId: 'XRP-USDT-SWAP', markPx: '1.2703', ts: String(Date.now()) }] : [] }) });
    }
    if (url.pathname.endsWith('/public/time')) return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: '0', data: [{ ts: String(Date.now()) }] }) });
    r.abort();
  });
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(BASE);
  await page.waitForSelector('#home-sim:not([hidden])');
  // 1580 XRP short from 1.2595 at the demo mark 1.2703: −17.06 USDT, as the OKX app shows.
  await page.waitForFunction(() => document.querySelector('#home-okx-positions').textContent.includes('-17.06 USDT'), null, { timeout: 20000 });
  assert.match(await page.locator('#home-okx-positions').innerText(), /标记价（实时）\s*1\.2703/);
  assert.equal(await page.locator('#pinned-equity').innerText(), '4,981.94');            // 4,980.83 + 18.17 − 17.06
  assert.ok(demoHeaders.length && demoHeaders.every(h => h === '1'));
  // Diagnostics panel: opens from the header status and probes each connection.
  await page.locator('#connection').click(); await page.waitForSelector('#diag:not([hidden])');
  assert.match(await page.locator('#diag-body').innerText(), /REST 备用最近成功 \d+ 秒前/);
  await page.locator('#diag-test').click();
  await page.waitForFunction(() => !document.querySelector('#diag-test-out').textContent.includes('测试中'), null, { timeout: 20000 });
  const out = await page.locator('#diag-test-out').innerText();
  assert.match(out, /模拟盘 REST：HTTP 200/); assert.match(out, /模拟盘 WS wspap\.okx\.com：(连接出错|连接关闭)/);
  await page.locator('#diag-close').click(); assert.ok(await page.locator('#diag').isHidden());
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('模拟盘标记价 REST 备用验收通过：WebSocket 全部失败时按模拟盘 REST 标记价计算浮盈（-17.06，与 OKX App 一致）、顶栏权益随之更新、请求带模拟盘标头、诊断面板可打开并逐项测试、pageerror=0。');
})().catch(e => { console.error(e); process.exit(1); });

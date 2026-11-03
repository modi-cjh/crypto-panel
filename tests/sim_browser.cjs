// Paper-trading page acceptance: PANEL_URL=http://127.0.0.1:8008/ node tests/sim_browser.cjs
// The paper-data feed is served from a fixture through route interception only.
const { chromium, devices } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const base = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'sim-dashboard.json'), 'utf8'));
let feed = { ...base, updated_at: new Date().toISOString() };
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ ...devices['iPhone 13'] });
  let mode = 'ok';
  await context.route(/okx\.com/, r => r.abort());
  await context.route('**/paper-data/dashboard.json*', r => mode === 'ok' ? r.fulfill({ body: JSON.stringify(feed), contentType: 'application/json' }) : r.fulfill({ status: 404, body: 'missing' }));
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(BASE);
  // The original bot snapshot is paused, so the paper accounts lead the home page.
  await page.waitForSelector('#home-sim:not([hidden])');
  assert.ok(await page.evaluate(() => document.documentElement.classList.contains('sim-primary')));
  assert.equal(await page.locator('#pause-banner').isVisible(), false);
  assert.equal(await page.locator('#sim-summary').isVisible(), false);
  assert.equal(await page.locator('#home-page > .execution-panel').isVisible(), false);
  assert.equal(await page.locator('#home-sim .sim-card').count(), 3);
  assert.match(await page.locator('#pinned-label-text').innerText(), /模拟盘合计/);
  assert.match(await page.locator('#pinned-equity').innerText(), /285\.25/);
  await page.locator('#legacy-toggle').click();
  assert.equal(await page.locator('#home-page > .execution-panel').isVisible(), true);   // original bot still reachable
  await page.locator('#legacy-toggle').click();
  assert.equal(await page.locator('#home-page > .execution-panel').isVisible(), false);
  await page.locator('[data-nav="sim"]').click();
  await page.waitForSelector('#sim-page:not([hidden]) .sim-card');
  assert.equal(await page.locator('[data-nav="sim"]').getAttribute('aria-current'), 'page');
  assert.equal(await page.locator('#sim-page .sim-card').count(), 3);
  assert.equal(await page.locator('#sim-curve polyline').count(), 3);
  assert.ok(await page.locator('#sim-trades li').count() > 0);
  assert.equal(await page.locator('#pause-banner').isVisible(), false); // the old bot's banner does not leak onto this page
  for (const width of [320, 360, 390]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow at ${width}`);
  }
  // The companion follows the paper accounts and narrates a newly closed trade.
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.state === 'idle');
  assert.match(await page.locator('#mascot-status').innerText(), /待命/);
  const next = structuredClone(feed), acct = next.accounts.mean_reversion;
  acct.trades.unshift({ inst: 'BTC-USDT-SWAP', side: 1, entry_time: new Date(Date.now() - 7200e3).toISOString(), exit_time: new Date(Date.now() - 600e3).toISOString(), entry: 100, exit: 99, pnl: -1.05, r: -1.05, reason: 'stop' });
  acct.closed_trades += 1; feed = next;
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.state === 'sad');
  await page.waitForFunction(() => document.querySelector('#toasts').textContent.includes('模拟盘 · 均值回归 平仓 BTC'));
  await page.locator('#mascot-touch').click(); await page.waitForSelector('#mascot-drawer[open]');
  assert.match(await page.locator('#companion-panel-status').innerText(), /合计权益/);
  assert.equal(await page.locator('.companion-accounts li').count(), 3);
  await page.keyboard.press('Escape');
  mode = 'missing'; await page.reload(); await page.waitForSelector('#sim-error:not([hidden])');
  assert.match(await page.locator('#sim-error').innerText(), /读取失败/);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('模拟交易页验收通过：首页以模拟盘为主（旧暂停横幅与旧区块收起、顶栏合计余额、可展开原主力盘）、导航、三账户卡片、三条资金曲线、持仓与成交、旧暂停横幅不外溢、320/360/390 无溢出、伙伴跟随模拟盘并播报平仓、读取失败提示、pageerror=0。');
})().catch(e => { console.error(e); process.exit(1); });

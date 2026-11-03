// Paper-trading page acceptance: PANEL_URL=http://127.0.0.1:8008/ node tests/sim_browser.cjs
// The paper-data feed is served from a fixture through route interception only.
const { chromium, devices } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const fixture = fs.readFileSync(path.join(__dirname, 'fixtures', 'sim-dashboard.json'), 'utf8');
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ ...devices['iPhone 13'] });
  let mode = 'ok';
  await context.route(/okx\.com/, r => r.abort());
  await context.route('**/paper-data/dashboard.json*', r => mode === 'ok' ? r.fulfill({ body: fixture, contentType: 'application/json' }) : r.fulfill({ status: 404, body: 'missing' }));
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(BASE);
  await page.waitForSelector('#sim-summary:not([hidden])');
  assert.match(await page.locator('#sim-summary').innerText(), /3 个账户/);
  await page.locator('#sim-summary').click();
  await page.waitForSelector('#sim-page:not([hidden]) .sim-card');
  assert.equal(await page.locator('[data-nav="sim"]').getAttribute('aria-current'), 'page');
  assert.equal(await page.locator('.sim-card').count(), 3);
  assert.equal(await page.locator('#sim-curve polyline').count(), 3);
  assert.ok(await page.locator('#sim-trades li').count() > 0);
  assert.equal(await page.locator('#pause-banner').isVisible(), false); // the old bot's banner does not leak onto this page
  for (const width of [320, 360, 390]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow at ${width}`);
  }
  mode = 'missing'; await page.reload(); await page.waitForSelector('#sim-error:not([hidden])');
  assert.match(await page.locator('#sim-error').innerText(), /读取失败/);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('模拟交易页验收通过：首页摘要、导航、三账户卡片、三条资金曲线、持仓与成交、旧暂停横幅不外溢、320/360/390 无溢出、读取失败提示、pageerror=0。');
})().catch(e => { console.error(e); process.exit(1); });

// Market page acceptance: PANEL_URL=http://127.0.0.1:8008/ node tests/market_browser.cjs
// OKX public REST is served from generated data (120 contracts); WebSockets and paper-data are blocked.
const { chromium, devices } = require('playwright');
const assert = require('node:assert/strict');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const now = Date.now();
const tickers = Array.from({ length: 120 }, (_, i) => ({ instId: `C${String(i).padStart(3, '0')}-USDT-SWAP`, last: '10', open24h: '9.5', volCcy24h: String((120 - i) * 1e6), ts: String(now) }));
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ ...devices['iPhone 13'] });
  const funding = [];
  await context.route(/wss?:\/\//, r => r.abort());
  await context.route('**/paper-data/**', r => r.abort());
  await context.route('https://www.okx.com/api/v5/**', r => {
    const url = new URL(r.request().url()), path = url.pathname.replace('/api/v5/', '');
    const data = path === 'market/tickers' ? tickers
      : path === 'public/open-interest' ? tickers.map(t => ({ instId: t.instId, oiCcy: '1000' }))
      : path === 'public/funding-rate' ? (funding.push({ id: url.searchParams.get('instId'), at: Date.now() }), [{ instId: url.searchParams.get('instId'), fundingRate: '0.0001', fundingTime: String(now + 3600e3) }])
      : [];
    r.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: '0', data }) });
  });
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(BASE + '#/market');
  await page.waitForFunction(() => document.querySelectorAll('#market-body tr').length === 100);
  assert.match(await page.locator('#market-title').innerText(), /前 100/);
  assert.match(await page.locator('#market-body tr').nth(99).innerText(), /C099/);   // ranked by turnover; C100+ excluded
  assert.equal(await page.locator('#market-body').getByText('C100', { exact: false }).count(), 0);
  // Funding for all 100 arrives through the shared throttled queue, never in a burst over OKX's limit (20 per 2 s).
  await page.waitForFunction(() => [...document.querySelectorAll('#market-body tr')].every(tr => tr.textContent.includes('0.0100%')), null, { timeout: 60000 });
  const ids = new Set(funding.map(f => f.id));
  assert.ok(ids.has('C000-USDT-SWAP') && ids.has('C099-USDT-SWAP'));
  const maxIn2s = Math.max(...funding.map(f => funding.filter(g => g.at >= f.at && g.at < f.at + 2000).length));
  assert.ok(maxIn2s <= 20, `burst of ${maxIn2s} funding requests in 2 s`);
  await page.locator('#search').fill('C05');
  assert.equal(await page.locator('#market-body tr').count(), 10);
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow at ${width}`);
  }
  assert.deepEqual(errors, []);
  await browser.close();
  console.log(`市场行情验收通过：前 100（按成交额，第 101 名起不显示）、资金费率全部到齐且 2 秒内最多 ${maxIn2s} 个请求、搜索、320/390 无溢出、pageerror=0。`);
})().catch(e => { console.error(e); process.exit(1); });

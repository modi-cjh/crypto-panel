// Phone on a network that blocks OKX (Cambodian 4G): PANEL_URL=http://127.0.0.1:8008/ node tests/fallback_browser.cjs
// Every OKX request (REST on www / aws, every WebSocket) fails. The server read-only API (/live.json, /tickers.json) is faked by route
// interception. Expected: account, positions and PnL from the server, the market from the server's tickers, and no red error anywhere.
const { chromium, devices } = require('playwright');
const assert = require('node:assert/strict');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const iso = ms => new Date(ms).toISOString().replace(/\.\d+Z$/, 'Z');
const now = Date.now();
const snapshot = (over = {}) => ({
  schema_version: 1, source: 'okx-live', trading_mode: 'live', updated_at: iso(now - 30e3),
  equity: 131.25, available: 100.5, day_start: 126.67, day_pnl: 4.58, day_pnl_pct: 3.6157, start_equity: 126.66, total_pnl: 4.59,
  paused: false, halted: false, dry_run: false, closed_trades: 1,
  positions: [{ inst: 'SOL-USDT-SWAP', side: 'short', contracts: 1.58, lever: 10, mgnMode: 'cross', avgPx: 72.43, markPx: 70.1, upl: 3.6814,
    margin: 11.1, ctVal: 1, ours: true, stop: 80.18, target: 48.09, strategy: 'evolved', champion: 's15653' }],
  history: [[now - 7200e3, 126.66], [now - 30e3, 131.25]], ...over });
const fills = { schema_version: 1, source: 'okx-live', fills: [{ ts: iso(now - 700e3), action: 'open', inst: 'SOL-USDT-SWAP', side: 'short', contracts: 1.58, price: 72.46, dry_run: false }], events: [] };
const trades = { schema_version: 1, source: 'okx-live', trades: [], stats: { overall: { trades: 0 } } };
const apiDoc = () => ({ schema_version: 1, source: 'okx-live-api', updated_at: iso(now - 30e3), api: { account: 'okx', refresh_s: 60 }, snapshot: snapshot(), fills, trades });
const tickers = () => ({ schema_version: 1, source: 'okx-public', updated_at: iso(Date.now() - 20e3), tickers: Array.from({ length: 120 }, (_, i) => ({
  instId: i === 0 ? 'BTC-USDT-SWAP' : i === 1 ? 'SOL-USDT-SWAP' : `C${i}-USDT-SWAP`, last: i === 0 ? 91000 : 10 + i, open24h: i === 0 ? 90000 : 10, volCcy24h: 1e6 - i * 10, ts: Date.now() })) });
const json = (r, body, extra = {}) => r.fulfill({ status: 200, contentType: 'application/json', headers: { 'access-control-allow-origin': '*', ...extra }, body: JSON.stringify(body) });

async function phone(browser, { api = true, github = true, awsWorks = false } = {}) {
  const context = await browser.newContext({ ...devices['iPhone 13'] });
  const hits = { api: 0, tickers: 0, github: 0, www: 0, aws: 0 };
  // 4G block: OKX REST hangs up, sockets are refused (both domains), unless awsWorks (only www blocked).
  await context.route(/www\.okx\.com/, r => { hits.www++; return r.abort('connectionrefused'); });
  await context.route(/aws\.okx\.com/, r => { hits.aws++; if (!awsWorks) return r.abort('connectionrefused');
    const p = new URL(r.request().url()).pathname;
    return json(r, { code: '0', data: p.endsWith('market/tickers') ? tickers().tickers.map(t => ({ ...t, last: String(t.last), open24h: String(t.open24h), volCcy24h: String(t.volCcy24h) })) : [] }); });
  await context.routeWebSocket(/okx\.com/, ws => ws.close());
  await context.route(/sslip\.io\/live\.json/, r => { hits.api++; return api ? json(r, apiDoc()) : r.abort('timedout'); });
  await context.route(/sslip\.io\/tickers\.json/, r => { hits.tickers++; return api ? json(r, tickers()) : r.abort('timedout'); });
  await context.route('**/live-data/snapshot.json*', r => { hits.github++; return github ? json(r, snapshot({ updated_at: iso(now - 40 * 60e3), equity: 127.1 })) : r.fulfill({ status: 404, body: 'x' }); });
  await context.route('**/live-data/fills.json*', r => github ? json(r, fills) : r.fulfill({ status: 404, body: 'x' }));
  await context.route('**/live-data/trades.json*', r => github ? json(r, trades) : r.fulfill({ status: 404, body: 'x' }));
  await context.route('**/paper-data/**', r => r.abort());
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  return { context, page, hits, errors };
}
const noRed = async (page, why) => {
  assert.equal(await page.locator('#status-pill').evaluate(el => el.classList.contains('error')), false, `${why}: status pill not red`);
  assert.doesNotMatch(await page.locator('#status-pill-label').innerText(), /读取失败/, `${why}: no 读取失败`);
  assert.equal(await page.locator('#live-error').isHidden(), true, `${why}: no live error`);
  assert.equal(await page.locator('#connection-dot').evaluate(el => el.classList.contains('error')), false, `${why}: connection dot not red`);
};

(async () => {
  const browser = await chromium.launch();

  // 1. OKX fully blocked, server API reachable: account from the API, prices from the server, no red.
  {
    const { context, page, hits, errors } = await phone(browser);
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelector('#live-equity')?.textContent.includes('131.25'));
    await page.waitForFunction(() => document.querySelector('#connection-text')?.textContent === '服务器行情', null, { timeout: 30000 });
    assert.ok(hits.www >= 1, 'tried www.okx.com');
    assert.ok(hits.aws >= 1, 'then aws.okx.com');
    assert.ok(hits.tickers >= 1, 'then the server tickers');
    assert.equal(hits.github, 0, 'API reachable: live-data not needed');
    assert.match(await page.locator('#live-meta').innerText(), /浮盈 \+3\.68 USDT（快照）/);
    assert.match(await page.locator('#live-positions details[data-inst="SOL-USDT-SWAP"] .okx-pos-pnl').innerText(), /\+3\.68/);
    assert.match(await page.locator('#live-fresh').innerText(), /刚刚/);
    assert.match(await page.locator('#status-pill-body').textContent(), /服务器只读接口/);
    assert.match(await page.locator('#status-pill-body').textContent(), /OKX 公共行情暂时连不上/);
    assert.match(await page.locator('#pinned-source').textContent(), /每分钟/);
    await noRed(page, 'OKX blocked');
    // Market page from the server tickers: rows, no funding / OI errors.
    await page.goto(BASE + '#/market');
    await page.waitForFunction(() => document.querySelectorAll('#market-body tr[data-coin]').length === 100, null, { timeout: 20000 });
    assert.match(await page.locator('#price-updated').innerText(), /服务器行情/);
    assert.match(await page.locator('#slow-updated').innerText(), /资金费率与持仓量暂停/);
    assert.match(await page.locator('#market-body tr[data-coin="BTC-USDT-SWAP"]').innerText(), /91,000/);
    await noRed(page, 'market from server');
    // Diagnostics name the source.
    await page.locator('#connection').click();
    assert.match(await page.locator('#diag-body').textContent(), /来源 服务器备用[\s\S]*实盘账户来源：服务器只读接口/);
    assert.deepEqual(errors, []);
    await context.close();
  }

  // 2. Only www.okx.com blocked: aws.okx.com serves the market (polling), still no red.
  {
    const { context, page, hits, errors } = await phone(browser, { awsWorks: true });
    await page.goto(BASE + '#/market');
    await page.waitForFunction(() => document.querySelectorAll('#market-body tr[data-coin]').length === 100, null, { timeout: 20000 });
    await page.waitForFunction(() => document.querySelector('#connection-text')?.textContent === '行情轮询');
    assert.doesNotMatch(await page.locator('#price-updated').innerText(), /服务器行情/);
    assert.ok(hits.aws >= 1);
    assert.equal(hits.tickers, 0, 'server tickers not needed');
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelector('#live-equity')?.textContent.includes('131.25'));
    await noRed(page, 'aws fallback');
    assert.deepEqual(errors, []);
    await context.close();
  }

  // 3. Server API down too: account from live-data silently (no red), prices: all sources failed -> only then the error.
  {
    const { context, page, hits, errors } = await phone(browser, { api: false });
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelector('#live-equity')?.textContent.includes('127.10'));
    assert.ok(hits.api >= 1 && hits.github >= 1);
    assert.equal(await page.locator('#live-error').isHidden(), true, 'live-data fallback is silent');
    assert.match(await page.locator('#status-pill-body').textContent(), /GitHub live-data/);
    assert.match(await page.locator('#pinned-source').textContent(), /每小时/);
    await page.waitForFunction(() => document.querySelector('#status-pill')?.classList.contains('error'), null, { timeout: 30000 });
    assert.match(await page.locator('#status-pill-body').textContent(), /行情：/);
    assert.equal(await page.locator('#connection-text').innerText(), '行情断开');
    assert.deepEqual(errors, []);
    await context.close();
  }

  await browser.close();
  console.log('fallback browser: ok');
})().catch(e => { console.error(e); process.exit(1); });

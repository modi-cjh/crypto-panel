// OKX live dashboard acceptance: PANEL_URL=http://127.0.0.1:8008/ node tests/live_browser.cjs
// live-data files are served through route interception; OKX REST is blocked, the public WebSocket is faked.
const { chromium, devices } = require('playwright');
const assert = require('node:assert/strict');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const iso = ms => new Date(ms).toISOString().replace(/\.\d+Z$/, 'Z');
const now = Date.now();
const snapshot = {
  schema_version: 1, source: 'okx-live', trading_mode: 'live', updated_at: iso(now - 600e3),
  equity: 126.82424, available: 109.49616, day_start: 126.670001, day_pnl: 0.154239, day_pnl_pct: 0.121764,
  start_equity: 126.66404, total_pnl: 0.1602, paused: false, halted: false, dry_run: false, closed_trades: 0,
  positions: [
    { inst: 'SOL-USDT-SWAP', side: 'short', contracts: 1.58, lever: 10, mgnMode: 'cross', avgPx: 72.43, markPx: 72.37, upl: 0.0948,
      uplRatio: 0.00828, margin: 11.43446, ctVal: 1, ours: true, stop: 80.18402, target: 48.08685, strategy: 'evolved', champion: 's15653', leader: false },
    { inst: 'IMX-USDT-SWAP', side: 'short', contracts: 200, lever: 10, mgnMode: 'cross', avgPx: 0.0995, markPx: 0.09929, upl: 0.042,
      uplRatio: 0.0211, margin: 1.9858, ctVal: 1, ours: true, stop: 0.13004, target: 0.055107, strategy: 'evolved', champion: 's15653', leader: false },
  ],
  history: [[now - 7200e3, 126.66], [now - 3600e3, 126.7], [now - 600e3, 126.82]],
};
const fills = { schema_version: 1, source: 'okx-live', updated_at: snapshot.updated_at,
  fills: [{ ts: iso(now - 700e3), action: 'open', inst: 'SOL-USDT-SWAP', side: 'short', contracts: 1.58, price: 72.46, stop: 80.18, target: 48.09, leverage: 10, strategy: 'evolved', dry_run: false }],
  events: [{ ts: iso(now - 700e3), action: 'skip', inst: 'BNB-USDT-SWAP', reason: 'same_side_risk', dry_run: false }] };

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ ...devices['iPhone 13'] });
  let mode = 'ok';
  const requests = [];
  context.on('request', r => requests.push(r.url()));
  await context.route(/okx\.com/, r => r.abort());
  const sockets = [], subs = [];
  await context.routeWebSocket(/ws\.okx\.com/, ws => { sockets.push(ws); ws.onMessage(m => { if (m === 'ping') return ws.send('pong'); const b = JSON.parse(m); if (b.op === 'subscribe') subs.push(...b.args); }); });
  await context.route('**/live-data/snapshot.json*', r => mode === 'ok' ? r.fulfill({ body: JSON.stringify(snapshot), contentType: 'application/json' }) : r.fulfill({ status: 404, body: 'missing' }));
  await context.route('**/live-data/fills.json*', r => mode === 'ok' ? r.fulfill({ body: JSON.stringify(fills), contentType: 'application/json' }) : r.fulfill({ status: 404, body: 'missing' }));
  await context.addInitScript(() => { try { localStorage.setItem('modi-companion-position', JSON.stringify({ side: 'right', bottom: 20, docked: false })); } catch {} });
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(BASE);

  await page.waitForFunction(() => document.querySelector('#live-equity')?.textContent.includes('126.82'));
  assert.equal(await page.locator('#live-status').innerText(), '实盘运行中');
  assert.match(await page.locator('#live-day').innerText(), /当日盈亏 \+0\.15 USDT（\+0\.12%）/);
  assert.match(await page.locator('#live-meta').innerText(), /可用 109\.50 USDT/);
  assert.match(await page.locator('#live-fresh').innerText(), /UTC\+7.*10 分钟前/);
  assert.equal(await page.locator('#live-pos-count').innerText(), '2');
  assert.match(await page.locator('#pinned-equity').innerText(), /126\.82/);
  // Expand a position: stop, target, margin, mode.
  await page.locator('#live-positions details[data-inst="SOL-USDT-SWAP"] summary').click();
  const body = await page.locator('#live-positions details[data-inst="SOL-USDT-SWAP"] .okx-pos-body').innerText();
  for (const s of ['止损', '80.18', '止盈', '48.087', '保证金', '11.43', '全仓', '72.43']) assert.ok(body.includes(s), s);
  assert.match(await page.locator('#live-fills').innerText(), /开仓 · SOL[\s\S]*空 1.58 张 @ 72.46 · 止损 80.18 · 止盈 48.09/);
  assert.match(await page.locator('#live-events').innerHTML(), /同向风险上限/);
  assert.match(await page.locator('#live-curve').innerHTML(), /<svg/);

  // Live-only: no simulated wording, no paper-data or paper/*.json requests, risk notice present.
  const text = await page.locator('body').innerText();
  assert.doesNotMatch(text, /模拟|纸面|paper/i);
  assert.match(text, /真实资金，风险自负，不构成投资建议/);
  assert.match(await page.title(), /OKX 实盘看板/);
  assert.match(await page.locator('footer').innerText(), /OKX 实盘看板/);
  assert.equal(requests.filter(u => /paper-data|\/paper\/|okx_demo|wspap/.test(u)).length, 0, 'no simulated-account requests');
  assert.ok(requests.some(u => /live-data\/snapshot\.json\?t=\d+/.test(u)), 'cache-busted snapshot request');

  // Public mark price for a held coin re-prices the floating PnL (short: price down = profit).
  await page.waitForFunction(() => true);
  await new Promise(r => setTimeout(r, 300));
  assert.ok(subs.some(s => s.channel === 'mark-price' && s.instId === 'SOL-USDT-SWAP'), 'subscribed to SOL mark price');
  const push = (instId, markPx) => sockets.at(-1).send(JSON.stringify({ arg: { channel: 'mark-price', instId }, data: [{ instId, markPx: String(markPx), ts: String(Date.now()) }] }));
  push('SOL-USDT-SWAP', 70.43); push('IMX-USDT-SWAP', 0.0995);
  await page.waitForFunction(() => /\+3\.16 USDT/.test(document.querySelector('#live-positions details[data-inst="SOL-USDT-SWAP"] .okx-pos-pnl')?.textContent || ''), null, { timeout: 5000 });
  assert.match(await page.locator('#live-meta').innerText(), /实时标记价/);
  // The expanded row stays open through the re-price.
  assert.equal(await page.locator('#live-positions details[data-inst="SOL-USDT-SWAP"]').getAttribute('open'), '');

  // Stale data: offline warning.
  const stale = await browser.newContext();
  await stale.route(/okx\.com/, r => r.abort());
  await stale.route('**/live-data/snapshot.json*', r => r.fulfill({ body: JSON.stringify({ ...snapshot, updated_at: iso(now - 4 * 3600e3) }), contentType: 'application/json' }));
  await stale.route('**/live-data/fills.json*', r => r.fulfill({ body: JSON.stringify(fills), contentType: 'application/json' }));
  const sp = await stale.newPage();
  await sp.goto(BASE);
  await sp.waitForFunction(() => document.querySelector('#live-status')?.textContent === '数据离线');
  assert.match(await sp.locator('#live-fresh').innerText(), /可能离线/);
  await stale.close();

  // Feed missing on first load: an alert, no crash, no blank page.
  const missing = await browser.newContext();
  await missing.route(/okx\.com/, r => r.abort());
  await missing.route('**/live-data/**', r => r.fulfill({ status: 404, body: 'missing' }));
  const mp = await missing.newPage(), merrors = [];
  mp.on('pageerror', e => merrors.push(String(e)));
  await mp.goto(BASE);
  await mp.waitForSelector('#live-error:not([hidden])');
  assert.match(await mp.locator('#live-error').innerText(), /实盘数据暂时读取失败/);
  assert.deepEqual(merrors, []);
  await missing.close();

  // Market and coin pages still work; the retired evolve route returns home.
  await page.goto(BASE + '#/market');
  await page.waitForSelector('#market-page:not([hidden])');
  assert.equal(await page.locator('#home-page').isHidden(), true);
  await page.goto(BASE + '#/coin/SOL-USDT-SWAP');
  await page.waitForSelector('#coin-page:not([hidden])');
  assert.match(await page.locator('#coin-title').innerText(), /SOL \/ USDT 永续/);
  await page.goto(BASE + '#/evolve');
  await page.waitForSelector('#home-page:not([hidden])');

  // Companion follows the live account.
  await page.locator('#mascot-touch').tap();
  await page.waitForSelector('#mascot-drawer[open]');
  assert.match(await page.locator('#companion-panel-status').innerText(), /实盘权益 USDT/);
  assert.equal(await page.locator('#companion-tab-team').count(), 0);

  assert.deepEqual(errors, []);
  await browser.close();
  console.log('live browser: ok');
})().catch(e => { console.error(e); process.exit(1); });

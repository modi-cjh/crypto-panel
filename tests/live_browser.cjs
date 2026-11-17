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
const trades = { schema_version: 1, source: 'okx-live', updated_at: snapshot.updated_at,
  trades: [{ inst: 'AVAX-USDT-SWAP', side: 'short', contracts: 5.4, leverage: 10, entry: 7.258, exit: 8.37, stop: 8.375, target: 4.248, pnl: -6.5,
    pnl_pct: -15.32, roe_pct: -153.2, r: -1.07, opened_at: iso(now - 9 * 3600e3), closed_at: iso(now - 3 * 3600e3), hold_minutes: 360,
    exit_type: 'sl', outcome: 'stop', strategy: 'evolved', champion: 's20001' }],
  stats: { overall: { trades: 1, with_pnl: 1, wins: 0, losses: 1, win_rate: 0, pnl: -6.5, avg_loss: 6.5, avg_r: -1.07, tp: 0, sl: 1, max_drawdown_closed: 6.5 },
    by_strategy: [{ key: 's20001', trades: 1, wins: 0, losses: 1, win_rate: 0, pnl: -6.5, avg_r: -1.07 }], equity_drawdown: { usdt: 1.2, pct: 0.95 } } };
const candles = Array.from({ length: 60 }, (_, i) => { const t = Math.floor((now - (59 - i) * 900e3) / 900e3) * 900e3, o = 72 + Math.sin(i / 5);
  return [String(t), o.toFixed(2), (o + .5).toFixed(2), (o - .5).toFixed(2), (o + .1).toFixed(2), '1000', '1000', '72000', i === 59 ? '0' : '1']; }).reverse();

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ ...devices['iPhone 13'] });
  let mode = 'ok';
  const requests = [];
  context.on('request', r => requests.push(r.url()));
  await context.route(/okx\.com/, r => r.abort()); await context.route(/sslip\.io/, r => r.abort());
  const sockets = [], subs = [];
  await context.routeWebSocket(/ws\.okx\.com/, ws => { sockets.push(ws); ws.onMessage(m => { if (m === 'ping') return ws.send('pong'); const b = JSON.parse(m); if (b.op === 'subscribe') subs.push(...b.args); }); });
  await context.route('**/live-data/snapshot.json*', r => mode === 'ok' ? r.fulfill({ body: JSON.stringify(snapshot), contentType: 'application/json' }) : r.fulfill({ status: 404, body: 'missing' }));
  await context.route('**/live-data/fills.json*', r => mode === 'ok' ? r.fulfill({ body: JSON.stringify(fills), contentType: 'application/json' }) : r.fulfill({ status: 404, body: 'missing' }));
  await context.route('**/live-data/trades.json*', r => mode === 'ok' ? r.fulfill({ body: JSON.stringify(trades), contentType: 'application/json' }) : r.fulfill({ status: 404, body: 'missing' }));
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
  // Phone tab bar: no ancestor box may be sticky / transformed / filtered (iOS Safari then drew the fixed bar mid-screen
  // over the open position card), and after scrolling to the open card it is still pinned to the bottom, not covering it.
  const tabBar = await page.evaluate(() => {
    const nav = document.querySelector('nav[aria-label="主导航"]'), bad = [];
    for (let e = nav.parentElement; e && e !== document.documentElement; e = e.parentElement) { const c = getComputedStyle(e);
      if (c.display !== 'contents' && (c.position === 'sticky' || c.transform !== 'none' || c.filter !== 'none' || c.backdropFilter !== 'none' || c.contain !== 'none' || c.willChange !== 'auto')) bad.push(e.tagName + '.' + e.className); }
    const card = document.querySelector('#live-positions details[data-inst="SOL-USDT-SWAP"]');
    scrollBy(0, card.getBoundingClientRect().bottom - (innerHeight - nav.offsetHeight) + 8);
    const n = nav.getBoundingClientRect(), r = card.getBoundingClientRect();
    return { bad, position: getComputedStyle(nav).position, bottomGap: Math.abs(innerHeight - n.bottom), cardBottom: r.bottom, navTop: n.top, scrolled: scrollY > 0 };
  });
  assert.equal(tabBar.position, 'fixed');
  assert.deepEqual(tabBar.bad, [], 'tab bar has no sticky / transformed / filtered ancestor');
  assert.ok(tabBar.scrolled && tabBar.bottomGap < 1, `tab bar pinned to the viewport bottom after scrolling (${JSON.stringify(tabBar)})`);
  assert.ok(tabBar.cardBottom <= tabBar.navTop, `open position card scrolls fully above the tab bar (${JSON.stringify(tabBar)})`);
  assert.equal(await page.locator('.header-bar').evaluate(e => getComputedStyle(e).position), 'sticky', 'top bar still sticks on a phone');
  await page.evaluate(() => scrollTo(0, 0));
  assert.match(await page.locator('#live-fills').innerText(), /开仓 · SOL[\s\S]*空 1.58 张 @ 72.46 · 止损 80.18 · 止盈 48.09/);
  assert.match(await page.locator('#live-events').innerHTML(), /同向风险上限/);
  assert.match(await page.locator('#live-curve').innerHTML(), /<svg/);

  // Header status light: green / 运行正常 for a 10-minute-old snapshot.
  assert.equal(await page.locator('#live-light').getAttribute('data-state'), 'ok');
  assert.match(await page.locator('#live-light').innerText(), /运行正常/);
  assert.ok(await page.locator('#live-light').isVisible(), 'status light visible on a phone');
  // Compact phone rows: entry / stop / target on the summary line.
  assert.match(await page.locator('#live-positions details[data-inst="SOL-USDT-SWAP"] .okx-pos-line').innerText(), /入 72\.43 · 损 80\.18\d* · 盈 48\.087/);
  // Statistics and closed trades.
  const stats = await page.locator('#live-stats').innerText();
  for (const s of ['已平仓', '1 笔', '胜率', '0.0%', '平均 R', '-1.07 R', '最大回撤', '0.95%', 's20001']) assert.ok(stats.includes(s), s);
  assert.equal(await page.locator('#live-trade-count').innerText(), '1');
  const row = await page.locator('#live-trades .trade-row').first().innerText();
  for (const s of ['AVAX', '7.258', '8.37', '-6.50 U', '-153.20%', '止损', '6 小时', 's20001']) assert.ok(row.includes(s), s);
  // K line from a position: OKX REST is blocked here, so the popup falls back to the price ladder (no crash).
  await page.locator('#live-positions [data-chart-inst="SOL-USDT-SWAP"]').click();
  await page.waitForSelector('#trade-chart[open]');
  await page.waitForSelector('#trade-chart-box.fallback svg');
  assert.match(await page.locator('#trade-chart-note').innerText(), /K 线读取失败.*价位参考/);
  assert.match(await page.locator('#trade-chart-levels').innerText(), /入场 72\.43[\s\S]*止损 80\.18[\s\S]*止盈 48\.08/);
  assert.equal(await page.locator('#live-positions details[data-inst="SOL-USDT-SWAP"]').getAttribute('open'), '', 'chart button does not toggle the row');
  await page.locator('#trade-chart-close').click();
  await page.waitForSelector('#trade-chart:not([open])', { state: 'attached' });
  // From a closed trade: same popup with the exit level.
  await page.locator('#live-trades .trade-row').first().click();
  await page.waitForSelector('#trade-chart[open] #trade-chart-box.fallback svg');
  assert.match(await page.locator('#trade-chart-title').innerText(), /AVAX 空 · 已平仓/);
  assert.match(await page.locator('#trade-chart-levels').innerText(), /出场 8\.37/);
  await page.keyboard.press('Escape');
  await page.waitForSelector('#trade-chart:not([open])', { state: 'attached' });

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
  await stale.route(/okx\.com/, r => r.abort()); await stale.route(/sslip\.io/, r => r.abort());
  await stale.route('**/live-data/snapshot.json*', r => r.fulfill({ body: JSON.stringify({ ...snapshot, updated_at: iso(now - 4 * 3600e3) }), contentType: 'application/json' }));
  await stale.route('**/live-data/fills.json*', r => r.fulfill({ body: JSON.stringify(fills), contentType: 'application/json' }));
  const sp = await stale.newPage();
  await sp.goto(BASE);
  await sp.waitForFunction(() => document.querySelector('#live-status')?.textContent === '数据离线');
  assert.match(await sp.locator('#live-fresh').innerText(), /可能离线/);
  assert.equal(await sp.locator('#live-light').getAttribute('data-state'), 'stale');
  await stale.close();

  // Desktop with public candles available: the popup draws a real chart (entry marker, stop / target lines).
  const desk = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await desk.route(/okx\.com/, r => r.abort()); await desk.route(/sslip\.io/, r => r.abort());   // registered first: the candle route below takes precedence
  await desk.route(/okx\.com\/api\/v5\/market\/(history-)?candles/, r => r.fulfill({ body: JSON.stringify({ code: '0', data: candles }), contentType: 'application/json' }));
  await desk.route('**/live-data/snapshot.json*', r => r.fulfill({ body: JSON.stringify({ ...snapshot, halted: true }), contentType: 'application/json' }));
  await desk.route('**/live-data/fills.json*', r => r.fulfill({ body: JSON.stringify(fills), contentType: 'application/json' }));
  await desk.route('**/live-data/trades.json*', r => r.fulfill({ status: 404, body: 'missing' }));   // older sync: no trades.json yet
  const dp = await desk.newPage(), derrors = [];
  dp.on('pageerror', e => derrors.push(String(e)));
  await dp.goto(BASE);
  await dp.waitForFunction(() => document.querySelector('#live-light')?.dataset.state === 'halted');
  assert.match(await dp.locator('#live-light').innerText(), /熔断/);
  assert.match(await dp.locator('#live-trades').innerText(), /还没有已平仓/);
  await dp.locator('#live-positions [data-chart-inst="SOL-USDT-SWAP"]').click();
  await dp.waitForFunction(() => /K 线 · 60 根/.test(document.querySelector('#trade-chart-note')?.textContent || ''), null, { timeout: 10000 });
  assert.ok(await dp.locator('#trade-chart-box canvas').count() > 0, 'chart canvas drawn');
  assert.deepEqual(derrors, []);
  await desk.close();

  // Feed missing on first load: an alert, no crash, no blank page.
  const missing = await browser.newContext();
  await missing.route(/okx\.com/, r => r.abort()); await missing.route(/sslip\.io/, r => r.abort());
  await missing.route('**/live-data/**', r => r.fulfill({ status: 404, body: 'missing' }));
  const mp = await missing.newPage(), merrors = [];
  mp.on('pageerror', e => merrors.push(String(e)));
  await mp.goto(BASE);
  await mp.waitForSelector('#live-error:not([hidden])');
  assert.match(await mp.locator('#live-error').innerText(), /实盘数据暂时读取失败/);
  assert.deepEqual(merrors, []);
  await missing.close();

  // Market, coin and 策略进化 pages work; the retired simulated-account routes return home.
  await page.goto(BASE + '#/market');
  await page.waitForSelector('#market-page:not([hidden])');
  assert.equal(await page.locator('#home-page').isHidden(), true);
  await page.goto(BASE + '#/coin/SOL-USDT-SWAP');
  await page.waitForSelector('#coin-page:not([hidden])');
  assert.match(await page.locator('#coin-title').innerText(), /SOL \/ USDT 永续/);
  await page.goto(BASE + '#/evolve');
  await page.waitForSelector('#evolve-page:not([hidden])');
  assert.equal(await page.locator('#home-page').isHidden(), true);
  await page.goto(BASE + '#/sim');
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

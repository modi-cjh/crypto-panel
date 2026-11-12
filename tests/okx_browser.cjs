// OKX demo-trading account acceptance: PANEL_URL=http://127.0.0.1:8008/ node tests/okx_browser.cjs
// The paper-data feed is served through route interception only; OKX public endpoints are blocked.
const { chromium, devices } = require('playwright');
const assert = require('node:assert/strict');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const iso = ms => new Date(ms).toISOString();
const now = Date.now();
let feed = {
  updated_at: iso(now - 300e3), equity: 5012.5, available: 4900, day_start: 5000, start_equity: 5000, paused: false, halted: false, dry_run: false, closed_trades: 1,
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
  // A returning visitor who brought the companion out keeps it floating (phones start with it docked).
  await context.addInitScript(() => { try { localStorage.setItem('modi-companion-position', JSON.stringify({ side: 'right', bottom: 20, docked: false })); } catch {} });
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
  assert.match(await page.locator('#home-okx-status').innerText(), /允许开仓/);
  assert.equal(await page.locator('#home-okx-curve polyline').count(), 1);
  assert.match(await page.locator('#home-okx-positions').innerText(), /趋势突破只做多/);
  // Home shows fills only; housekeeping (skips, pauses) is folded into the run log.
  assert.match(await page.locator('#home-okx-actions').innerText(), /开仓 · BTC/);
  assert.doesNotMatch(await page.locator('#home-okx-actions').innerText(), /信号太旧，价格已离开入场区间/);
  await page.locator('#home-okx-log').evaluate(el => el.closest('details').open = true);
  assert.match(await page.locator('#home-okx-log').innerText(), /信号太旧，价格已离开入场区间/);
  // Positions are compact rows: PnL visible, details one tap away.
  assert.equal(await page.locator('#home-okx-pos-count').innerText(), '1');
  assert.equal(await page.locator('#home-okx-positions details[open]').count(), 0);
  assert.match(await page.locator('#home-okx-positions summary').innerText(), /BTC[\s\S]*多 5×[\s\S]*\+12\.50 USDT/);
  // The notices collapse into one line that expands on tap.
  assert.match(await page.locator('[data-notes-summary]').first().innerText(), /快照 .*分钟前|快照 刚刚/);
  assert.equal(await page.locator('#home-sim [data-stop-demo]').count(), 1);            // stop control is on 交易室 (模拟交易 was merged in)
  assert.equal(paperRequests, 0, 'the paper account feed is no longer read');
  // With prices unchanged (OKX is blocked here) the equity numbers must not animate on each refresh.
  await page.evaluate(() => { window.__rolls = 0; for (const id of ['pinned-equity', 'okx-total-equity'])
    new MutationObserver(m => { for (const r of m) for (const n of r.addedNodes) if (n.classList?.contains('number-in')) { window.__rolls++; window.__lastRoll = { id: r.target.id, moved: n.textContent, still: r.target.firstChild?.textContent }; } }).observe(document.getElementById(id), { childList: true }); });
  await page.waitForTimeout(20000);
  assert.equal(await page.evaluate(() => window.__rolls), 0, 'equity animated without a change');
  // Floating PnL follows the demo market's own mark price (not the live exchange): 3 contracts × 0.01 BTC × (70,500 − 70,000).
  // (the 20 s quiet period above lets the stream's no-data watchdog reconnect, so there can be more than one subscribe)
  assert.ok(demoSubs.length >= 1 && demoSubs.every(x => x.channel === 'mark-price' && x.instId === 'BTC-USDT-SWAP'));
  await page.evaluate(() => { document.querySelector('#home-okx-positions details').dataset.keep = '1'; });
  pushMark('BTC-USDT-SWAP', 70500);
  await page.waitForFunction(() => document.querySelector('#home-okx-positions').textContent.includes('+15.00 USDT'));
  // Price ticks patch the row in place: same element, icon not reloaded (no flashing).
  assert.equal(await page.evaluate(() => document.querySelector('#home-okx-positions details').dataset.keep), '1');
  await page.locator('#home-okx-positions summary').first().click();                  // expand; the open state survives re-renders
  await page.waitForTimeout(1500);
  assert.equal(await page.locator('#home-okx-positions details[open]').count(), 1);
  assert.match(await page.locator('#home-okx-positions').innerText(), /标记价（实时）\s*70,500/);
  await page.waitForFunction(() => document.querySelector('#pinned-equity').textContent.trim() === '5,015.00');   // 5,012.50 − 12.50 + 15.00
  // Gentle update: only the changed digits animate (5,01|2.50 → 5,01|5.00); the leading digits stay still.
  assert.deepEqual(await page.evaluate(() => ({ moved: window.__lastRoll.moved, still: window.__lastRoll.still })), { moved: '5.00', still: '5,01' });
  // A burst of ticks (10 in 2 s) leaves the curve, trades list and rows untouched and rolls the equity at most once.
  await page.evaluate(() => { for (const sel of ['#home-okx-curve svg', '#home-okx-actions li', '#home-okx-positions details']) document.querySelector(sel).dataset.keep2 = '1';
    window.__burstRolls = 0; new MutationObserver(m => { for (const r of m) for (const n of r.addedNodes) if (n.classList?.contains('number-in')) window.__burstRolls++; }).observe(document.getElementById('okx-total-equity'), { childList: true }); });
  for (let i = 1; i <= 10; i++) { pushMark('BTC-USDT-SWAP', 70500 + i * 10); await page.waitForTimeout(200); }
  await page.waitForTimeout(1500);
  assert.deepEqual(await page.evaluate(() => ['#home-okx-curve svg', '#home-okx-actions li', '#home-okx-positions details'].map(sel => document.querySelector(sel).dataset.keep2)), ['1', '1', '1']);
  assert.ok(await page.evaluate(() => window.__burstRolls) <= 1, 'equity animated more than once in a burst');
  assert.match(await page.locator('#home-okx-positions summary').innerText(), /\+18\.00 USDT/);   // still converges to the latest mark (70,600)
  // Exchange REST/WebSockets are blocked here, so the top-bar status comes from the demo price feed alone.
  await page.waitForFunction(() => document.querySelector('#connection-text').textContent === '行情实时', null, { timeout: 12000 });
  await page.locator('#legacy-toggle').click();
  assert.equal(await page.locator('#home-page > .execution-panel').isVisible(), true);   // original bot still reachable
  await page.locator('#legacy-toggle').click();
  // 账户历史 and 模拟交易 are both merged into 交易室: no nav link for either, and old links land on the home page.
  assert.equal(await page.locator('[data-nav="paper"], #paper-page, [data-nav="sim"], #sim-page').count(), 0);
  for (const old of ['#/paper', '#/sim']) {
    await page.evaluate(h => { location.hash = h; }, old);
    await page.waitForFunction(() => location.hash === '#/');
  }
  await page.waitForSelector('#home-page:not([hidden]) #home-sim:not([hidden])');
  assert.equal(await page.locator('[data-nav="home"]').getAttribute('aria-current'), 'page');
  assert.match(await page.locator('#okx-total-ret').innerText(), /已平仓 1 笔/);
  assert.match(await page.locator('#okx-compare').innerText(), /纸面|等待纸面账户/);            // 跟单对照 now on 交易室
  assert.equal(await page.locator('#home-page [data-stop-demo]').count(), 1);                  // so is 停止交易
  assert.match(await page.locator('#home-page .panel-caption').first().innerText(), /规则：/);
  for (const width of [320, 360, 390]) {
    await page.setViewportSize({ width, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `overflow at ${width}`);
  }
  // The companion follows the OKX account and announces a new exchange action.
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.state === 'excited');   // just opened BTC
  // At work: watching the open position with a small trading screen tinted by live PnL; the chip says so.
  await page.waitForFunction(() => document.querySelector('#mascot-status').textContent === '盯盘 · 1 仓');
  assert.equal(await page.locator('#mascot').getAttribute('data-work'), 'watch');
  assert.equal(await page.locator('#mascot').getAttribute('data-tone'), 'up');
  assert.ok(await page.locator('#mascot .work-monitor').isVisible());
  // Double tap: a spoken position report.
  await page.locator('#mascot-touch').click(); await page.locator('#mascot-touch').click();
  await page.waitForFunction(() => /持仓 1 个：BTC 多 \+18\.00U/.test(document.querySelector('#mascot-bubble').textContent));
  // A new open pops a ticket above the head and the robot presses its button.
  feed = { ...feed, updated_at: iso(Date.now()), recent_actions: [{ ts: iso(Date.now()), action: 'open', inst: 'ETH-USDT-SWAP', side: 1, contracts: 2, price: 3000, stop: 2900, leverage: 5, dry_run: false }, ...feed.recent_actions] };
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForSelector('#mascot-ticket:not([hidden])[data-kind="open-long"]');
  assert.match(await page.locator('#mascot-ticket').innerText(), /开多 ETH[\s\S]*2 张 @ 3000/);
  assert.equal(await page.locator('#mascot-status').innerText(), '开仓中');
  assert.equal(await page.locator('#mascot').getAttribute('data-reaction'), 'trade');
  await page.waitForSelector('#mascot-ticket[hidden]', { state: 'attached', timeout: 6000 });
  feed = { ...feed, updated_at: iso(Date.now()), positions: [], open_mirrors: [], closed_trades: 2,
    recent_actions: [{ ts: iso(Date.now()), action: 'closed_on_exchange', inst: 'BTC-USDT-SWAP', dry_run: false }, ...feed.recent_actions] };
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.state === 'sad');
  // The stop-or-target fill shows its PnL from the last live mark (+18.00 U, so a target): coins, then back to waiting.
  await page.waitForSelector('#mascot-ticket:not([hidden])[data-kind="close-win"]');
  assert.match(await page.locator('#mascot-ticket').innerText(), /BTC 止盈触发 \+18\.00 U/);
  await page.waitForFunction(() => /^(等信号|扫描信号)/.test(document.querySelector('#mascot-status').textContent), null, { timeout: 8000 });
  await page.waitForFunction(() => document.querySelector('#toasts').textContent.includes('OKX 模拟 · BTC 交易所止损 / 止盈触发'));
  assert.match(await page.locator('#home-okx-positions').innerText(), /当前没有持仓/);
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

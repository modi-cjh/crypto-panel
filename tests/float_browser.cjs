// Desktop floating window: PANEL_URL=http://127.0.0.1:8008/ node tests/float_browser.cjs
// Document Picture-in-Picture is stubbed with a popup window (same API surface: a separate document).
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const iso = ms => new Date(ms).toISOString(), now = Date.now();
const feed = { updated_at: iso(now - 60e3), equity: 4900, available: 3000, day_start: 5000, start_equity: 5000, paused: false, halted: false, closed_trades: 0,
  history: [[now - 3600e3, 5000], [now - 60e3, 4900]],
  positions: [{ inst: 'BTC-USDT-SWAP', side: 'long', contracts: '15', avgPx: '77450', markPx: '76900', upl: '-82.5', lever: '10', margin: '1160', ctVal: 0.01, ours: true }],
  open_mirrors: [], recent_actions: [] };
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  await context.addInitScript(() => {
    try { localStorage.setItem('modi-companion-position', JSON.stringify({ side: 'right', bottom: 20, docked: false })); } catch {}
    window.documentPictureInPicture = { requestWindow: async ({ width, height }) => window.open('about:blank', 'pip', `width=${width},height=${height}`) };
  });
  await context.route(/okx\.com/, r => r.abort());
  await context.routeWebSocket(/okx\.com/, ws => ws.close());
  await context.route('**/okx_demo/summary.json*', r => r.fulfill({ contentType: 'application/json', body: JSON.stringify(feed) }));
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(BASE);
  await page.waitForFunction(() => document.querySelector('#mascot-status').textContent === '盯盘 · 1 仓');
  await page.locator('#mascot-touch').click(); await page.waitForSelector('#mascot-drawer[open]');
  assert.ok(await page.locator('#companion-float').isVisible(), 'float button offered where the API exists');
  const [pip] = await Promise.all([context.waitForEvent('page'), page.locator('#companion-float').click()]);
  await pip.waitForSelector('#mascot.in-pip');
  assert.equal(await page.locator('#mascot').count(), 0, 'the robot moved out of the page');
  await pip.waitForFunction(() => document.querySelector('[data-pip-equity]').textContent === '4,900.00 U');
  assert.match(await pip.locator('[data-pip-line]').innerText(), /今日 -100\.00 · 盯盘 · 1 仓/);
  assert.equal(await pip.locator('#mascot').getAttribute('data-work'), 'watch');
  assert.ok(await pip.locator('#mascot .work-monitor').isVisible());
  await pip.waitForFunction(() => getComputedStyle(document.body).display === 'flex', null, { timeout: 10000 });   // page styles carried over
  assert.equal(await pip.locator('#mascot .acc-zzz').isVisible(), false);                                        // accessory rules apply
  // Tapping in the mini window reads out the positions instead of opening the page panel.
  await pip.locator('#mascot-touch').click();
  await pip.waitForFunction(() => /持仓 1 个：BTC 多/.test(document.querySelector('#mascot-bubble').textContent));
  // Closing the mini window brings the robot back to the page.
  await pip.close();
  await page.waitForSelector('#mascot:not(.in-pip)');
  assert.equal(await page.locator('#mascot').count(), 1);
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('桌面悬浮窗验收通过：面板提供“桌面悬浮窗”、机器人移入置顶小窗并保持盯盘状态与行情屏、显示权益与今日盈亏、样式随行、点按播报持仓、关闭后回到页面、pageerror=0。');
})().catch(e => { console.error(e); process.exit(1); });

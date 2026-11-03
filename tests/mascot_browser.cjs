// Companion acceptance: PANEL_URL=http://127.0.0.1:8008/ node tests/mascot_browser.cjs
// Synthetic responses are injected only through route interception; nothing is written to paper/.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const read = name => JSON.parse(fs.readFileSync(path.join(root, name), 'utf8'));
const iso = t => new Date(t).toISOString();

function paused() { const a = read('paper/account.json'); a.updated_at = iso(Date.now()); return a; }
function running(fills = []) {
  const a = paused(), demo = read('tests/fixtures/home.json'), now = Date.now();
  delete a.pause;
  Object.assign(a.accounts[0], { status: '运行中', updated_at: iso(now), balance: 100, equity: 101.4, daily_loss_limit: .08,
    day: new Date(now + 7 * 3600e3).toISOString().slice(0, 10), day_start: 100.5, daily_pnl: .9, strategy_enabled: true,
    positions: demo.positions.map(p => ({ ...p, entry_time: iso(now - 3 * 3600e3) })), equity_curve: [], fills });
  return a;
}
const visible = (page, sel) => page.locator(sel).isVisible();
const wait = (page, ms) => page.waitForTimeout(ms);

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  let account = paused(), team = { schema_version: 1, status: { updated_at: null, meeting: null, phase: null, roles: {} }, log: [] };
  await context.route(/okx\.com/, r => r.abort());
  await context.route('**/paper/account.json?*', r => r.fulfill({ json: account }));
  await context.route('**/paper/team.json?*', r => r.fulfill({ json: team }));
  await context.route('**/paper/events.json?*', r => r.fulfill({ json: { schema_version: 1, generated_at: iso(Date.now()), source_status: 'ok', events: [] } }));
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(BASE);
  await page.waitForSelector('#mascot[data-state="paused"]');
  const host = page.locator('#mascot');

  // Floating on every route, never inside the home page flow.
  assert.equal(await page.locator('#home-page #mascot').count(), 0);
  assert.equal(await host.evaluate(e => getComputedStyle(e).position), 'fixed');
  assert.equal(await host.getAttribute('data-eyes'), 'closed');
  assert.match(await host.getAttribute('data-acc'), /zzz/);
  // Greeting bubble appears once data is valid.
  await page.waitForFunction(() => !document.querySelector('#mascot-bubble').hidden);
  assert.match(await page.locator('#mascot-bubble').innerText(), /MODI/);
  // Errors collapse into one compact line.
  assert.ok(await page.locator('#alerts .alert').count() <= 1);

  // Single tap opens the panel; tabs work by click and arrow keys; Esc returns focus.
  await page.locator('#mascot-touch').tap(); await page.waitForSelector('#mascot-drawer[open]');
  assert.equal(await page.locator('#mascot-touch').getAttribute('aria-expanded'), 'true');
  assert.match(await page.locator('#companion-tip').innerText(), /暂停/);
  assert.match(await page.locator('#companion-panel-status').innerText(), /100\.00/);
  await page.locator('#companion-tab-status').focus(); await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#companion-tab-team').getAttribute('aria-selected'), 'true');
  assert.equal(await page.locator('#mascot-report-list').innerText(), '暂无报告');
  await page.keyboard.press('ArrowRight');
  assert.equal(await visible(page, '#companion-panel-feed'), true);
  await page.keyboard.press('Escape');
  assert.equal(await visible(page, '#mascot-drawer'), false);
  assert.ok(await page.locator('#mascot-touch').evaluate(e => e === document.activeElement));
  // Keyboard activation opens immediately.
  await page.keyboard.press('Enter'); await page.waitForSelector('#mascot-drawer[open]');
  await page.locator('[data-close]').click();

  // Double tap greets without opening; triple tap makes it dizzy.
  const box = await page.locator('#mascot-touch').boundingBox(), cx = box.x + box.width / 2, cy = box.y + box.height / 2;
  for (let i = 0; i < 2; i++) { await page.touchscreen.tap(cx, cy); await wait(page, 60); }
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.reaction === 'wave');
  assert.equal(await visible(page, '#mascot-drawer'), false);
  for (let i = 0; i < 3; i++) { await page.touchscreen.tap(cx, cy); await wait(page, 60); }
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.eyes === 'dizzy');
  assert.equal(await visible(page, '#mascot-drawer'), false);
  // Long press shows love and does not open the panel.
  await page.mouse.move(cx, cy); await page.mouse.down(); await wait(page, 700); await page.mouse.up(); await wait(page, 350);
  assert.match(await host.getAttribute('data-acc'), /heart/);
  assert.equal(await visible(page, '#mascot-drawer'), false);

  // Drag snaps to the left edge, persists across reload, and never opens the panel.
  await page.mouse.move(cx, cy); await page.mouse.down(); await page.mouse.move(60, 400, { steps: 8 }); await page.mouse.up(); await wait(page, 400);
  assert.equal(await host.getAttribute('data-side'), 'left');
  assert.equal(await visible(page, '#mascot-drawer'), false);
  const saved = JSON.parse(await page.evaluate(() => localStorage.getItem('modi-companion-position')));
  assert.equal(saved.side, 'left');
  await page.reload(); await page.waitForSelector('#mascot[data-state="paused"]');
  assert.equal(await host.getAttribute('data-side'), 'left');
  assert.equal(await host.evaluate(e => e.style.left), '12px');

  // Dock to the edge from the panel; a tap brings it back.
  await page.locator('#mascot-touch').click(); await page.waitForSelector('#mascot-drawer[open]');
  await page.locator('#companion-dock').click();
  assert.ok(await host.evaluate(e => e.classList.contains('docked')));
  await page.locator('#mascot-touch').click({ force: true });
  assert.ok(!(await host.evaluate(e => e.classList.contains('docked'))));

  // Route tips follow navigation.
  await page.goto(BASE + '#/coin/BTC-USDT-SWAP'); await page.waitForSelector('#mascot[data-state]');
  await page.locator('#mascot-touch').click(); await page.waitForSelector('#mascot-drawer[open]');
  assert.match(await page.locator('#companion-tip').innerText(), /BTC/);
  await page.keyboard.press('Escape');

  // Live narration: a new fill is spoken in the bubble and logged in the feed.
  account = running(); await page.goto(BASE); await page.waitForSelector('#mascot[data-state]');
  await wait(page, 1500);
  account = running([{ timestamp: iso(Date.now()), symbol: 'ETH-USDT-SWAP', side: 1, kind: 'open', qty: .05, price: 2450.5, notional: .05 * 2450.5, pnl: null }]);
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.state === 'excited');
  await page.waitForFunction(() => [...document.querySelectorAll('#mascot-bubble,#toasts')].some(e => e.textContent.includes('开仓 ETH 多')));
  assert.match(await page.locator('#toasts').innerText(), /新成交/);

  // Reports are escaped.
  team = { schema_version: 1, generated_at: iso(Date.now()), status: { updated_at: iso(Date.now()), meeting: null, phase: null,
    roles: { risk: { status: '工作中', task: '复盘', last_report: { ts: iso(Date.now()), title: '新报告 <script>', summary: '公开摘要', grade: 'A-' } } } }, log: [] };
  await page.reload(); await page.waitForSelector('#mascot[data-state]');
  await page.locator('#mascot-touch').click(); await page.waitForSelector('#mascot-drawer[open]');
  await page.locator('#companion-tab-team').click();
  await page.waitForFunction(() => document.querySelector('#mascot-report-list').textContent.includes('<script>'));
  assert.equal(await page.locator('#mascot-report-list script').count(), 0);
  await page.keyboard.press('Escape');

  // Reduced motion and low-end devices stop every animation.
  await page.emulateMedia({ reducedMotion: 'reduce' });
  assert.ok(await host.evaluate(e => e.classList.contains('still')));
  assert.equal(await page.locator('#mascot .m-float').evaluate(e => getComputedStyle(e).animationName), 'none');
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  for (const prop of ['hardwareConcurrency', 'deviceMemory']) {
    const low = await context.newPage();
    await low.addInitScript(`Object.defineProperty(navigator,'${prop}',{get:()=>2})`);
    await low.goto(BASE); await low.waitForSelector('#mascot[data-state]');
    assert.ok(await low.locator('#mascot').evaluate(e => e.classList.contains('still')));
    await low.close();
  }
  // No horizontal overflow on narrow phones, on every route.
  for (const hash of ['#/', '#/paper', '#/market']) for (const width of [320, 360, 390]) {
    await page.setViewportSize({ width, height: 844 }); await page.goto(BASE + hash); await page.waitForSelector('#mascot[data-state]');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${hash} ${width}`);
  }
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('伙伴验收通过：全局悬浮、表情、问候、单击面板/方向键/Esc/Enter、双击挥手、连点眩晕、长按比心、拖动吸边与恢复、收进边缘、页面提示、新成交播报、报告转义、reduced-motion/低端设备、320/360/390 无溢出、pageerror=0。');
})().catch(e => { console.error(e); process.exit(1); });

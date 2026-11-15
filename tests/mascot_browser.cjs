// Companion acceptance: PANEL_URL=http://127.0.0.1:8008/ node tests/mascot_browser.cjs
// Synthetic live-data responses are injected only through route interception.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
const read = name => JSON.parse(fs.readFileSync(path.join(root, name), 'utf8'));
const iso = t => new Date(t).toISOString();

const LIVE = { schema_version: 1, source: 'okx-live', trading_mode: 'live', equity: 100, available: 100, day_start: 100, start_equity: 100,
  halted: false, dry_run: false, positions: [], history: [] };
function paused() { return { ...LIVE, updated_at: iso(Date.now()), paused: true }; }
function running() { return { ...LIVE, updated_at: iso(Date.now()), paused: false, equity: 101.4, day_start: 100.5,
  positions: [{ inst: 'ETH-USDT-SWAP', side: 'long', contracts: 5, lever: 10, mgnMode: 'cross', avgPx: 2400, markPx: 2410, upl: .5, margin: 12, ctVal: .01, ours: true }] }; }
const fillsDoc = rows => ({ schema_version: 1, source: 'okx-live', updated_at: iso(Date.now()), fills: rows, events: [] });
const visible = (page, sel) => page.locator(sel).isVisible();
const wait = (page, ms) => page.waitForTimeout(ms);

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  let account = paused(), fills = fillsDoc([]);
  await context.route(/okx\.com/, r => r.abort()); await context.route(/sslip\.io/, r => r.abort());
  await context.routeWebSocket(/okx\.com/, ws => ws.close());
  await context.route('**/live-data/snapshot.json*', r => r.fulfill({ json: account }));
  await context.route('**/live-data/fills.json*', r => r.fulfill({ json: fills }));
  // First visit on a phone starts docked at the edge; a returning visitor's choice is kept.
  { const fresh = await context.newPage(); await fresh.goto(BASE); await fresh.waitForSelector('#mascot');
    assert.ok(await fresh.locator('#mascot').evaluate(e => e.classList.contains('docked')), 'phones start docked'); await fresh.close(); }
  await context.addInitScript(() => { try { if (!sessionStorage.getItem('seeded')) { localStorage.setItem('modi-companion-position', JSON.stringify({ side: 'right', bottom: 20, docked: false })); sessionStorage.setItem('seeded', '1'); } } catch {} });
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
  // The live dashboard has two tabs: 状态 and 动态 (no team / decisions feeds).
  assert.equal(await page.locator('#companion-tab-team').count(), 0);
  assert.equal(await page.locator('#companion-tab-decisions').count(), 0);
  await page.locator('#companion-tab-status').focus(); await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#companion-tab-feed').getAttribute('aria-selected'), 'true');
  assert.equal(await visible(page, '#companion-panel-feed'), true);
  await page.keyboard.press('Escape');
  assert.equal(await visible(page, '#mascot-drawer'), false);
  assert.ok(await page.locator('#mascot-touch').evaluate(e => e === document.activeElement));
  // Pointer use never leaves a keyboard focus ring on the robot or in the panel.
  await page.locator('#mascot-touch').tap(); await page.waitForSelector('#mascot-drawer[open]');
  assert.equal(await page.locator('[data-close]').evaluate(e => getComputedStyle(e).outlineStyle), 'none');
  await page.locator('[data-close]').tap();
  assert.equal(await page.locator('#mascot-touch').evaluate(e => e === document.activeElement && getComputedStyle(e).outlineStyle), 'none');
  // Without any keypress the ring stays off even if the class logic never ran (stale cached script).
  assert.equal(await page.evaluate(() => document.documentElement.classList.contains('companion-keyboard')), false);
  await page.keyboard.press('Shift'); // any key brings the ring back for keyboard users
  assert.notEqual(await page.locator('#mascot-touch').evaluate(e => getComputedStyle(e).outlineStyle), 'none');
  // Keyboard activation opens immediately.
  await page.keyboard.press('Enter'); await page.waitForSelector('#mascot-drawer[open]');
  await page.locator('[data-close]').click();

  // Double tap reads out the live positions (or greets) without opening; triple tap makes it dizzy.
  const box = await page.locator('#mascot-touch').boundingBox(), cx = box.x + box.width / 2, cy = box.y + box.height / 2;
  for (let i = 0; i < 2; i++) { await page.touchscreen.tap(cx, cy); await wait(page, 60); }
  await page.waitForFunction(() => ['wave', 'report'].includes(document.querySelector('#mascot').dataset.reaction));
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
  assert.equal(Math.round((await host.boundingBox()).x), 12);

  // Touch drag, including holding still first (iOS long-press) and an OS-cancelled gesture.
  const cdp = await context.newCDPSession(page), touch = (type, pts) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: pts });
  for (const [hold, endType, target] of [[700, 'touchEnd', 'right'], [0, 'touchCancel', 'left']]) {
    const r = await page.locator('#mascot-touch').boundingBox(), x = r.x + r.width / 2, y = r.y + r.height / 2;
    const toX = target === 'right' ? 330 : 40;
    await touch('touchStart', [{ x, y }]); await wait(page, hold);
    for (let i = 1; i <= 10; i++) await touch('touchMove', [{ x: x + (toX - x) * i / 10, y: y - 20 * i }]);
    await touch(endType, []); await wait(page, 400);
    assert.equal(await host.getAttribute('data-side'), target, `touch drag hold=${hold} ${endType}`);
    assert.equal(await visible(page, '#mascot-drawer'), false);
  }
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

  // Live narration: a new fill shows a ticket, is spoken and toasted.
  account = running(); fills = fillsDoc([]); await page.goto(BASE); await page.waitForSelector('#mascot[data-state]');
  await wait(page, 1500);
  fills = fillsDoc([{ ts: iso(Date.now()), action: 'open', inst: 'ETH-USDT-SWAP', side: 'long', contracts: 5, price: 2450.5, stop: 2300, leverage: 10, dry_run: false }]);
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForFunction(() => document.querySelector('#mascot').dataset.state === 'excited');
  await page.waitForFunction(() => [...document.querySelectorAll('#mascot-bubble,#toasts,#mascot-ticket')].some(e => e.textContent.includes('开多 ETH')));
  await page.locator('#mascot-touch').click(); await page.waitForSelector('#mascot-drawer[open]');
  await page.locator('#companion-tab-feed').click();
  assert.match(await page.locator('#companion-feed').innerText(), /开仓 ETH/);
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
  for (const hash of ['#/', '#/coin/BTC-USDT-SWAP', '#/market']) for (const width of [320, 360, 390]) {
    await page.setViewportSize({ width, height: 844 }); await page.goto(BASE + hash); await page.waitForSelector('#mascot[data-state]');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${hash} ${width}`);
  }
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('伙伴验收通过：全局悬浮、表情、问候、单击面板/方向键/Esc/Enter、双击挥手、连点眩晕、长按比心、拖动吸边与恢复、收进边缘、页面提示、新成交播报、报告转义、reduced-motion/低端设备、320/360/390 无溢出、pageerror=0。');
})().catch(e => { console.error(e); process.exit(1); });

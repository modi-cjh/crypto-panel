// Home-screen app acceptance: PANEL_URL=http://127.0.0.1:8008/ node tests/app_browser.cjs
// Chrome reports the parsed manifest and installability errors over the DevTools protocol.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const BASE = process.env.PANEL_URL || 'http://127.0.0.1:8008/';
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await context.route(/okx\.com/, r => r.abort());
  const page = await context.newPage(), errors = [], blocked = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (/Content Security Policy|manifest/i.test(m.text()) && m.type() === 'error') blocked.push(m.text()); });
  await page.goto(BASE); await page.waitForLoadState('load');
  const cdp = await context.newCDPSession(page);
  const { url, errors: parseErrors, data } = await cdp.send('Page.getAppManifest');
  assert.match(url, /manifest\.webmanifest$/); assert.deepEqual(parseErrors, []);
  const m = JSON.parse(data);
  assert.equal(m.short_name, 'MODI'); assert.equal(m.display, 'standalone'); assert.equal(m.start_url, './?source=app');
  for (const icon of m.icons) {
    const r = await page.request.get(new URL(icon.src, url).href);
    assert.equal(r.status(), 200, icon.src); assert.equal(r.headers()['content-type'], 'image/png');
  }
  const touch = await page.locator('link[rel="apple-touch-icon"]').getAttribute('href');
  assert.equal((await page.request.get(new URL(touch, BASE).href)).status(), 200);
  assert.equal(await page.locator('meta[name="apple-mobile-web-app-capable"]').getAttribute('content'), 'yes');
  assert.equal(await page.locator('meta[name="apple-mobile-web-app-title"]').getAttribute('content'), 'MODI');
  const { installabilityErrors } = await cdp.send('Page.getInstallabilityErrors');
  assert.deepEqual(installabilityErrors.map(e => e.errorId), [], JSON.stringify(installabilityErrors));
  // Launching from the icon (start_url with ?source=app) loads the normal dashboard.
  await page.goto(new URL(m.start_url, url).href); await page.waitForSelector('#mascot');
  assert.deepEqual(blocked, []); assert.deepEqual(errors, []);
  await browser.close();
  console.log('主屏幕 App 验收通过：manifest 解析无误（MODI · standalone）、图标 192/512/maskable 与 apple-touch-icon 可访问、iOS 全屏标签齐全、Chrome 无安装性错误、从图标启动正常、无 CSP 拦截、pageerror=0。');
})().catch(e => { console.error(e); process.exit(1); });

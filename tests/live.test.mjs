import test from 'node:test';
import assert from 'node:assert/strict';
import { LIVE_SNAPSHOT_URL, LIVE_FILLS_URL, validLiveSnapshot, validLiveFills, liveView, liveFreshness, liveStatus, liveCurveSVG,
  livePositionsMarkup, liveFillsMarkup, liveNewFills, liveFeed, liveMascotState, fillDetail } from '../assets/live.mjs';

const NOW = Date.parse('2026-11-15T15:00:00Z');
const snap = (over = {}) => ({
  schema_version: 1, source: 'okx-live', trading_mode: 'live', updated_at: '2026-11-15T14:02:46Z',
  equity: 126.82424, available: 109.49616, day_start: 126.670001, day_pnl: 0.154239, day_pnl_pct: 0.121764,
  start_equity: 126.66404, paused: false, halted: false, dry_run: false, closed_trades: 0,
  positions: [
    { inst: 'SOL-USDT-SWAP', side: 'short', contracts: 1.58, lever: 10, mgnMode: 'cross', avgPx: 72.43, markPx: 72.37, upl: 0.0948,
      uplRatio: 0.00828, margin: 11.43446, ctVal: 1, ours: true, stop: 80.18402, target: 48.08685, strategy: 'evolved', champion: 's15653', leader: false },
    { inst: 'BTC-USDT-SWAP', side: 'long', contracts: 1, lever: 5, mgnMode: 'cross', avgPx: 90000, markPx: 91000, upl: 10, margin: 180, ctVal: 0.01, ours: false },
  ],
  history: [[1794751218461, 126.66], [1794751366879, 126.82]],
  ...over,
});
const fillsDoc = (rows, events = []) => ({ schema_version: 1, source: 'okx-live', updated_at: '2026-11-15T14:02:46Z', fills: rows, events });
const OPEN = { ts: '2026-11-15T14:00:18Z', action: 'open', inst: 'SOL-USDT-SWAP', side: 'short', contracts: 1.58, price: 72.46, stop: 80.18, target: 48.09, leverage: 10, strategy: 'evolved', dry_run: false };
const CLOSE = { ts: '2026-11-15T14:30:00Z', action: 'closed_on_exchange', inst: 'AVAX-USDT-SWAP', side: 'short', pnl: -1.25, close_price: 8.4, outcome: 'stop', dry_run: false };

test('urls point at the live-data branch, not paper-data', () => {
  assert.equal(LIVE_SNAPSHOT_URL, 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/live-data/snapshot.json');
  assert.equal(LIVE_FILLS_URL, 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/live-data/fills.json');
});

test('validation', () => {
  assert.ok(validLiveSnapshot(snap()));
  assert.ok(!validLiveSnapshot(snap({ trading_mode: 'demo' })));
  assert.ok(!validLiveSnapshot(snap({ updated_at: 'nope' })));
  assert.ok(!validLiveSnapshot(snap({ positions: [{ inst: '<b>', side: 'long' }] })));
  assert.ok(!validLiveSnapshot(null));
  assert.ok(validLiveFills(fillsDoc([OPEN], [])));
  assert.ok(!validLiveFills(fillsDoc([{ action: 'open' }])));
  assert.ok(!validLiveFills({}));
});

test('view: snapshot values without marks, live marks re-price PnL and equity', () => {
  const d = snap(), v = liveView(d, new Map(), NOW);
  assert.equal(v.live, false);
  assert.equal(v.equity, 126.82424);
  assert.ok(Math.abs(v.day - (126.82424 - 126.670001)) < 1e-9);
  assert.ok(Math.abs(v.upl - 10.0948) < 1e-9);
  const marks = new Map([['SOL-USDT-SWAP', { price: 72.0, at: NOW - 1000 }], ['BTC-USDT-SWAP', { price: 92000, at: NOW - 1000 }]]);
  const m = liveView(d, marks, NOW);
  assert.equal(m.live, true);
  const sol = m.positions.find(p => p.inst === 'SOL-USDT-SWAP'), btc = m.positions.find(p => p.inst === 'BTC-USDT-SWAP');
  assert.ok(Math.abs(sol.upl - 1.58 * (72.43 - 72.0)) < 1e-9);          // short gains when price falls
  assert.ok(Math.abs(btc.upl - 0.01 * (92000 - 90000)) < 1e-9);
  assert.ok(Math.abs(m.equity - (126.82424 - 10.0948 + sol.upl + btc.upl)) < 1e-9);
  // Stale marks are ignored.
  assert.equal(liveView(d, new Map([['SOL-USDT-SWAP', { price: 1, at: NOW - 60000 }]]), NOW).positions[0].live, false);
});

test('freshness and status', () => {
  assert.equal(liveFreshness(snap(), NOW).stale, false);
  assert.match(liveFreshness(snap(), NOW).text, /11\/15 21:02.*UTC\+7.*57 分钟前/);
  const late = Date.parse('2026-11-15T17:00:00Z');
  assert.equal(liveFreshness(snap(), late).stale, true);
  assert.match(liveFreshness(snap(), late).text, /可能离线/);
  assert.equal(liveStatus(snap(), NOW).label, '实盘运行中');
  assert.equal(liveStatus(snap({ dry_run: true }), NOW).label, '空跑 · 不下单');
  assert.equal(liveStatus(snap({ paused: true }), NOW).label, '已暂停');
  assert.equal(liveStatus(snap({ halted: true }), NOW).label, '日亏停手');
  assert.equal(liveStatus(snap(), late).label, '数据离线');
  assert.equal(liveStatus(null).label, '等待数据');
});

test('positions markup shows every live field, stop and target, and marks manual positions', () => {
  const d = snap(), html = livePositionsMarkup(d, liveView(d, new Map(), NOW));
  for (const s of ['SOL', '空 10×', '1.58', '全仓', '72.43', '72.37', '80.18', '48.087', '11.43', 's15653', '人工仓位（程序不管理）', '+0.09 USDT'])
    assert.ok(html.includes(s), s);
  assert.doesNotMatch(html, /模拟/);
  assert.match(livePositionsMarkup(snap({ positions: [] }), liveView(snap({ positions: [] }))), /当前没有持仓/);
});

test('fills markup and escaping', () => {
  const html = liveFillsMarkup([CLOSE, OPEN]);
  assert.match(html, /交易所止损触发 · AVAX/);
  assert.match(html, /已实现 -1.25 USDT/);
  assert.match(html, /开仓 · SOL/);
  assert.match(fillDetail(OPEN), /空 1.58 张 @ 72.46 · 止损 80.18 · 止盈 48.09 · 10×/);
  assert.match(liveFillsMarkup([]), /还没有成交/);
  const evil = liveFillsMarkup([{ ts: OPEN.ts, action: '<img src=x>', inst: 'SOL-USDT-SWAP' }]);
  assert.doesNotMatch(evil, /<img/);
  assert.match(liveFillsMarkup([{ ts: OPEN.ts, action: 'skip', inst: 'BNB-USDT-SWAP', reason: 'same_side_risk' }]), /同向风险上限/);
});

test('new fills feed the companion tickets (side as 1 / -1, oldest first)', () => {
  const prev = fillsDoc([OPEN]), next = fillsDoc([CLOSE, OPEN]);
  assert.deepEqual(liveNewFills(prev, next).map(a => [a.action, a.side]), [['closed_on_exchange', -1]]);
  assert.deepEqual(liveNewFills(null, next), []);
  assert.equal(liveFeed(next).length, 2);
});

test('companion state', () => {
  assert.equal(liveMascotState(null).state, 'offline');
  assert.equal(liveMascotState(snap({ halted: true }), null, NOW).state, 'paused');
  assert.equal(liveMascotState(snap(), fillsDoc([OPEN]), NOW).state, 'excited');
  assert.equal(liveMascotState(snap(), fillsDoc([CLOSE]), NOW).state, 'sad');
  assert.equal(liveMascotState(snap(), fillsDoc([]), NOW).state, 'happy');
});

test('curve', () => {
  assert.match(liveCurveSVG(snap()), /<svg[^>]+OKX 实盘账户资金曲线/);
  assert.match(liveCurveSVG(snap({ history: [] })), /资金曲线会在/);
});

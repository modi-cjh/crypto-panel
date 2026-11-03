import test from 'node:test';
import assert from 'node:assert/strict';
import { validOkx, okxLive, okxTotals, okxStatus, okxCurveSVG, okxPositionsMarkup, okxActionsMarkup, okxMascotState, okxChanges, okxFeed, okxStart } from '../assets/okx.mjs';

const NOW = Date.parse('2026-11-03T12:00:00Z');
const summary = (over = {}) => ({
  updated_at: '2026-11-03T11:40:00Z', equity: 5030, available: 4800, day_start: 5000, start_equity: 5000, paused: false, halted: false, closed_trades: 2,
  history: [[NOW - 7200e3, 5000], [NOW - 3600e3, 5010], [NOW - 1200e3, 5030]],
  positions: [{ inst: 'BTC-USDT-SWAP', side: 'long', contracts: '3', avgPx: '70000', markPx: '70100', upl: '3', lever: '5', margin: '420', ctVal: 0.01, ours: true }],
  open_mirrors: [{ inst: 'BTC-USDT-SWAP', strategy: 'breakout_long', stop: 69000 }],
  recent_actions: [{ ts: '2026-11-03T11:30:00Z', action: 'open', inst: 'BTC-USDT-SWAP', strategy: 'breakout_long', side: 1, contracts: 3, price: 70000, stop: 69000, leverage: 5 },
    { ts: '2026-11-03T08:00:00Z', action: 'skip', inst: 'XRP-USDT-SWAP', reason: 'paper_entry_too_old' }],
  ...over,
});

test('summary is validated before it is shown', () => {
  assert.ok(validOkx(summary()));
  assert.ok(validOkx(summary({ history: undefined })));
  assert.equal(validOkx(summary({ equity: 'x' })), false);
  assert.equal(validOkx(summary({ positions: [{ inst: '<x>' }] })), false);
  assert.equal(validOkx(summary({ history: [['a', 1]] })), false);
  assert.equal(validOkx(null), false);
});

test('floating PnL follows live prices with the contract value, else the recorded values', () => {
  const live = okxLive(summary(), [{ id: 'BTC-USDT-SWAP', last: 70500 }], NOW - 1000, NOW);
  assert.ok(live.live); assert.equal(live.positions[0].upl, 15);            // 3 contracts × 0.01 BTC × 500
  assert.equal(live.equity, 5030 - 3 + 15);
  const stale = okxLive(summary(), [{ id: 'BTC-USDT-SWAP', last: 70500 }], NOW - 60000, NOW);
  assert.equal(stale.live, false); assert.equal(stale.equity, 5030); assert.equal(stale.positions[0].upl, 3);
  const short = okxLive(summary({ positions: [{ inst: 'ETH-USDT-SWAP', side: 'net', contracts: '-2', avgPx: '3000', upl: '0', ctVal: 0.1, lever: '5' }] }), [{ id: 'ETH-USDT-SWAP', last: 2900 }], NOW, NOW);
  assert.equal(short.positions[0].side, -1); assert.equal(short.positions[0].upl, 20);
});

test('totals report return against the starting equity and today against the day start', () => {
  const t = okxTotals(summary(), [], 0, NOW);
  assert.equal(t.ret.toFixed(2), '0.60'); assert.equal(t.day, 30); assert.equal(t.positions, 1); assert.equal(t.trades, 2);
  assert.equal(okxStart(summary({ start_equity: undefined })), 5000);
  assert.equal(okxStart(summary({ start_equity: undefined, history: [] })), 5000);
});

test('status reflects stale, halted, paused and running', () => {
  assert.equal(okxStatus(summary(), NOW).label, '运行中');
  assert.equal(okxStatus(summary({ paused: true }), NOW).label, '已暂停');
  assert.equal(okxStatus(summary({ halted: true, paused: true }), NOW).label, '日亏停手');
  assert.equal(okxStatus(summary(), NOW + 6 * 3600e3).label, '数据延迟');
});

test('curve draws the equity history and waits for enough points', () => {
  assert.match(okxCurveSVG(summary()), /<polyline/);
  assert.match(okxCurveSVG(summary({ history: [[NOW, 5000]] })), /资金曲线会在/);
});

test('positions and actions are labelled in Chinese and exchange text is escaped', () => {
  const pos = okxPositionsMarkup(summary(), [], 0, NOW);
  assert.match(pos, /趋势突破只做多/); assert.match(pos, /69,000\.0/); assert.match(pos, /\+3\.00 USDT/);
  assert.match(okxPositionsMarkup(summary({ positions: [{ ...summary().positions[0], ours: false }] }), [], 0, NOW), /非本程序仓位/);
  assert.match(okxPositionsMarkup(summary({ positions: [] })), /当前没有持仓/);
  const acts = okxActionsMarkup(summary({ recent_actions: [...summary().recent_actions, { ts: '2026-11-03T07:00:00Z', action: 'error', inst: '-', detail: '<b>bad</b>' }] }));
  assert.match(acts, /信号已超过 2 小时/); assert.match(acts, /多 3 张 @ 70000/); assert.match(acts, /&lt;b&gt;bad/); assert.doesNotMatch(acts, /<b>bad/);
});

test('companion follows the account and new actions are announced once', () => {
  assert.equal(okxMascotState(summary(), NOW).state, 'excited');
  assert.equal(okxMascotState(summary({ recent_actions: [] }), NOW).state, 'happy');
  assert.equal(okxMascotState(summary({ recent_actions: [{ ts: '2026-11-03T11:50:00Z', action: 'closed_on_exchange', inst: 'BTC-USDT-SWAP' }] }), NOW).state, 'sad');
  assert.equal(okxMascotState(summary({ paused: true }), NOW).state, 'paused');
  assert.equal(okxMascotState(summary(), NOW + 6 * 3600e3).state, 'offline');
  const prev = summary(), next = summary({ recent_actions: [{ ts: '2026-11-03T12:00:00Z', action: 'close', inst: 'BTC-USDT-SWAP', reason: 'paper_closed' }, ...prev.recent_actions] });
  assert.deepEqual(okxChanges(prev, next).map(l => l.text), ['平仓 BTC · 策略信号平仓']);
  assert.deepEqual(okxChanges(null, next), []);
  assert.equal(okxFeed(next).length, 2);                                     // skips are not narrated
});

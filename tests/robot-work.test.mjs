import test from 'node:test';
import assert from 'node:assert/strict';
import { workState, nextBarClose, pnlMood, tradeBurst, positionReport, pnlComment } from '../assets/robot-work.mjs';

const H = 3600000, base = Date.parse('2026-11-04T08:00:00Z');
const pos = [{ inst: 'BTC-USDT-SWAP', side: 1, upl: -75.7, ours: false }, { inst: 'XRP-USDT-SWAP', side: -1, upl: 12.3, ours: true }];

test('work mode: watching positions, scanning after an hourly close, otherwise waiting with a countdown', () => {
  const w = workState({ positions: pos, equity: 4900 }, base + 20 * 60000);
  assert.equal(w.mode, 'watch'); assert.equal(w.label, '盯盘 · 2 仓'); assert.equal(w.tone, 'down');
  assert.match(w.detail, /合计浮盈 -63\.40 U/); assert.match(w.detail, /波动最大的是 BTC/);
  assert.equal(workState({ positions: [] }, base + 2 * 60000).mode, 'scan');
  const wait = workState({ positions: [], equity: 5000 }, base + 20 * 60000);
  assert.equal(wait.mode, 'wait'); assert.equal(wait.label, '等信号 · 40 分');
  assert.equal(nextBarClose(base + 59 * 60000 + 30000), 30000);
  assert.equal(workState({ positions: [], paused: true }, base + 20 * 60000).mode, 'paused');
  assert.equal(workState({ positions: pos, halted: true }).mode, 'paused');
  assert.equal(workState({ offline: true }).mode, 'offline');
  assert.match(workState({ positions: pos, paused: true }, base).detail, /新开仓已暂停/);   // still watching what is open
});

test('mood follows floating PnL relative to equity', () => {
  assert.equal(pnlMood(15, 5000), 'happy');       // +0.3%
  assert.equal(pnlMood(-30, 5000), 'worried');    // -0.6%
  assert.equal(pnlMood(-5, 5000), null);
  assert.equal(pnlMood(NaN, 5000), null);
});

test('trade tickets for opens and closes, with PnL from the fill or the last floating value', () => {
  assert.deepEqual(tradeBurst({ action: 'open', inst: 'BTC-USDT-SWAP', side: 1, contracts: 3, price: 70000 }),
    { kind: 'open-long', title: '开多 BTC', detail: '3 张 @ 70000', mood: 'excited' });
  assert.equal(tradeBurst({ action: 'open', inst: 'XRP-USDT-SWAP', side: -1, contracts: 15.8, price: 1.26 }).title, '开空 XRP');
  const win = tradeBurst({ action: 'close', inst: 'BTC-USDT-SWAP', net_pnl: 2.5 });
  assert.equal(win.kind, 'close-win'); assert.equal(win.amount, '+2.50 U'); assert.equal(win.detail, '含手续费');
  const stop = tradeBurst({ action: 'closed_on_exchange', inst: 'ETH-USDT-SWAP' }, new Map([['ETH-USDT-SWAP', -48.2]]));
  assert.equal(stop.kind, 'close-loss'); assert.equal(stop.title, 'ETH 止损触发'); assert.equal(stop.amount, '-48.20 U'); assert.equal(stop.mood, 'sad');
  assert.equal(tradeBurst({ action: 'closed_on_exchange', inst: 'ETH-USDT-SWAP' }, new Map([['ETH-USDT-SWAP', 9]])).title, 'ETH 止盈触发');
  assert.equal(tradeBurst({ action: 'close', inst: 'SOL-USDT-SWAP' }).kind, 'close');            // PnL unknown: no amount
  assert.equal(tradeBurst({ action: 'resume', inst: '-' }), null);
});

test('double-tap report lists positions by size of PnL', () => {
  assert.equal(positionReport(pos), '持仓 2 个：BTC 多 -75.70U、XRP 空 +12.30U；合计 -63.40U。');
  assert.match(positionReport([]), /现在空仓/);
});

test('PnL commentary only on large moves and at most every two minutes', () => {
  const memo = { upl: null, at: 0 }, eq = 5000;
  assert.equal(pnlComment(memo, { upl: -20, equity: eq, positions: pos }, base), null);              // baseline
  assert.equal(pnlComment(memo, { upl: -25, equity: eq, positions: pos }, base + 200000), null);     // < 10 U (0.2%)
  assert.equal(pnlComment(memo, { upl: -40, equity: eq, positions: pos }, base + 60000), null);      // too soon
  const c = pnlComment(memo, { upl: -40, equity: eq, positions: pos }, base + 130000);
  assert.equal(c.mood, 'worried'); assert.match(c.text, /浮亏扩大到 -40\.00U，BTC 拖累最多/);
  const up = pnlComment(memo, { upl: -10, equity: eq, positions: pos }, base + 300000);
  assert.equal(up.mood, 'happy'); assert.match(up.text, /XRP 出力最多/);
  assert.equal(pnlComment(memo, { upl: 0, equity: eq, positions: [] }, base + 900000), null); assert.equal(memo.upl, null);
});

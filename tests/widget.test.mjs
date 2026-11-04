import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// The Scriptable widget is a plain script; outside Scriptable it only defines its functions.
const ctx = {}; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(new URL('../widgets/modi-robot.js', import.meta.url), 'utf8'), ctx);
const model = (...a) => ctx.widgetModel(...a);
const T = Date.parse('2026-11-04T08:20:00Z');
const summary = (over = {}) => ({ updated_at: '2026-11-04T08:00:00Z', equity: 4900, day_start: 5000, paused: false, halted: false,
  positions: [{ inst: 'BTC-USDT-SWAP', side: 'long', contracts: '15', avgPx: '77450', markPx: '76900', upl: '-82.5', ctVal: 0.01 },
              { inst: 'XRP-USDT-SWAP', side: 'short', contracts: '15.8', avgPx: '1.2595', markPx: '1.268', upl: '-13.4', ctVal: 100 }],
  recent_actions: [], ...over });

test('widget marks positions to live demo prices and falls back to the snapshot', () => {
  const live = model(summary(), { 'BTC-USDT-SWAP': 77550, 'XRP-USDT-SWAP': 1.2595 }, T);
  assert.equal(live.live, true); assert.equal(live.positions[0].coin, 'BTC'); assert.equal(Math.round(live.positions[0].upl * 100) / 100, 15);
  assert.equal(Math.round(live.equity * 100) / 100, 4900 + 82.5 + 13.4 + 15);
  const snap = model(summary(), {}, T);
  assert.equal(snap.live, false); assert.equal(snap.equity, 4900); assert.equal(snap.day, -100);
  assert.equal(snap.positions[0].coin, 'BTC');                              // biggest PnL first
});

test('widget shows what the robot is doing and its mood', () => {
  const w = model(summary(), {}, T);
  assert.equal(w.mode, 'watch'); assert.equal(w.label, '盯盘 · 2 仓'); assert.equal(w.mood, 'worried');   // -96 U on 4,900
  assert.equal(model(summary({ positions: [] }), {}, Date.parse('2026-11-04T09:02:00Z')).label, '扫描信号');
  assert.equal(model(summary({ positions: [], updated_at: '2026-11-04T08:10:00Z' }), {}, T).label, '等信号 · 40 分');
  assert.equal(model(summary({ positions: [], recent_actions: [{ ts: '2026-11-04T08:05:00Z', action: 'open', inst: 'SOL-USDT-SWAP' }] }), {}, T).mood, 'excited');
  assert.equal(model(summary({ updated_at: '2026-11-04T05:00:00Z' }), {}, T).label, '快照延迟');
  assert.equal(model(summary({ halted: true }), {}, T).mood, 'sleepy');
  assert.equal(model(null, {}, T).mode, 'offline');
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { validSim, livePnl, simTotals, simSummaryMarkup, simCardsMarkup, simCurveSVG, simPositionsMarkup, simTradesMarkup, simMascotState, simChanges, simFeed, simLiveSince, simBackfillNote } from '../assets/sim.mjs';

const now = Date.parse('2026-11-03T15:00:00Z');
const account = (over = {}) => ({ name: '日线趋势突破', rules: '20 日新高', equity: 101.5, return_pct: 1.5, max_drawdown_pct: .8, closed_trades: 1, win_rate_pct: 100, avg_r: 1.5,
  payoff: null, halted: false, last_bar: '2026-11-03T00:00:00Z', curve: [[now - 7200000, 100], [now - 3600000, 101.5]],
  open_positions: [{ inst: 'BTC-USDT-SWAP', side: 1, entry: 100, stop: 90, target: null, qty: 1, margin: 5, leverage: 4, opened: '2026-11-03T01:00:00Z' }],
  trades: [{ inst: 'ETH-USDT-SWAP', side: 1, entry_time: '2026-11-02T00:00:00Z', exit_time: '2026-11-03T00:00:00Z', entry: 1, exit: 1.1, pnl: 1.5, r: 1.5, reason: 'stop' }], ...over });
const data = (accounts = { breakout_daily: account() }) => ({ schema_version: 1, updated_at: '2026-11-03T14:35:47Z', start_equity: 100, accounts });

test('validSim accepts the published shape and rejects malformed rows', () => {
  assert.equal(validSim(data()), true);
  assert.equal(validSim({ ...data(), schema_version: 2 }), false);
  assert.equal(validSim(data({ x: account({ open_positions: [{ ...account().open_positions[0], inst: 'BTC/USDT' }] }) })), false);
  assert.equal(validSim(data({ x: account({ curve: [[1.5, 100]] }) })), false);
  assert.equal(validSim(data({ x: account({ trades: [{ ...account().trades[0], pnl: 'NaN' }] }) })), false);
  assert.equal(validSim(data({})), false);
});

test('live PnL uses only fresh prices and is capped at the posted margin', () => {
  const p = account().open_positions[0];
  assert.deepEqual(livePnl(p, [{ id: 'BTC-USDT-SWAP', last: 103 }], now - 1000, now), { mark: 103, pnl: 3, pct: 60 });
  assert.equal(livePnl(p, [{ id: 'BTC-USDT-SWAP', last: 50 }], now - 1000, now).pnl, -5);
  assert.equal(livePnl({ ...p, side: -1 }, [{ id: 'BTC-USDT-SWAP', last: 98 }], now - 1000, now).pnl, 2);
  assert.equal(livePnl(p, [{ id: 'BTC-USDT-SWAP', last: 103 }], now - 30000, now).pnl, null); // stale poll
  assert.equal(livePnl(p, [], now, now).mark, null);
});

test('totals, cards, positions, trades and curve render escaped content', () => {
  const two = data({ breakout_daily: account(), mean_reversion: account({ name: '均值<script>', equity: 98, open_positions: [], trades: [] }) });
  const t = simTotals(two);
  assert.equal(t.count, 2); assert.equal(t.positions, 1); assert.ok(Math.abs(t.equity - 199.5) < 1e-9); assert.ok(Math.abs(t.ret - -.25) < 1e-9);
  assert.match(simSummaryMarkup(two), /2 个账户/);
  const cards = simCardsMarkup(two);
  assert.match(cards, /均值&lt;script&gt;/); assert.doesNotMatch(cards, /<script>/);
  assert.match(simPositionsMarkup(two, [{ id: 'BTC-USDT-SWAP', last: 103 }], now - 1000, now), /\+3\.00 USDT \/ \+60\.00%/);
  assert.match(simPositionsMarkup(data({ x: account({ open_positions: [] }) }), [], 0, now), /当前没有持仓/);
  assert.match(simTradesMarkup(two), /ETH/); assert.match(simTradesMarkup(data({ x: account({ trades: [] }) })), /还没有平仓记录/);
  assert.match(simCurveSVG(two), /<polyline/);
  assert.match(simCurveSVG(data({ x: account({ curve: [[now, 100]] }) })), /第一根新 K 线后出现/);
});

test('companion state follows the paper accounts: stale > halt > recent stop > recent open > profit > idle', () => {
  const fresh = { ...data(), updated_at: new Date(now - 600e3).toISOString() };
  const state = d => simMascotState(d, now).state;
  assert.equal(state(null), 'offline');
  assert.equal(state({ ...fresh, updated_at: new Date(now - 4 * 3600e3).toISOString() }), 'offline');
  assert.equal(state(fresh), 'happy');  // +1.5%, nothing recent
  assert.equal(state({ ...fresh, accounts: { a: account({ equity: 99, return_pct: -1, trades: [] }) } }), 'idle');
  const recentOpen = { ...fresh, accounts: { a: account({ open_positions: [{ ...account().open_positions[0], opened: new Date(now - 3600e3).toISOString() }] }) } };
  assert.equal(state(recentOpen), 'excited');
  const recentStop = { ...fresh, accounts: { a: account({ trades: [{ ...account().trades[0], pnl: -1, exit_time: new Date(now - 3600e3).toISOString() }] }) } };
  assert.equal(state(recentStop), 'sad'); assert.match(simMascotState(recentStop, now).message, /止损 -1\.00 USDT/);
  assert.equal(state({ ...recentStop, accounts: { a: account({ halted: true }) } }), 'paused');
});

test('simChanges narrates new opens, closes and halts once; simFeed lists them newest first', () => {
  const before = data(), after = structuredClone(before), a = after.accounts.breakout_daily;
  a.open_positions.push({ inst: 'SOL-USDT-SWAP', side: 1, entry: 150.123456789, stop: 140, target: null, qty: .1, margin: 3, leverage: 5, opened: '2026-11-03T14:00:00Z' });
  a.trades.unshift({ inst: 'XRP-USDT-SWAP', side: 1, entry_time: '2026-11-03T01:00:00Z', exit_time: '2026-11-03T14:00:00Z', entry: 1, exit: .9, pnl: -1.2, r: -1.2, reason: 'stop' });
  a.halted = true;
  const lines = simChanges(before, after);
  assert.deepEqual(lines.map(l => l.mood), ['excited', 'sad', 'sad']);
  assert.match(lines[0].text, /日线趋势突破 开仓 SOL 多 @ 150\.123/); assert.match(lines[1].text, /平仓 XRP 多 -1\.20 USDT/); assert.match(lines[2].text, /停手/);
  assert.deepEqual(simChanges(after, after), []); assert.deepEqual(simChanges(null, after), []);
  const feed = simFeed(after);
  assert.equal(feed.length, 4); assert.ok(feed.every((r, i) => i === 0 || feed[i - 1].ts >= r.ts));
});

test('combined curve starts where every account has data and carries earlier values forward', () => {
  const hourly = account({ curve: [[now - 7200e3, 100], [now - 3600e3, 101]] });
  const daily = account({ curve: [[now - 86400e3, 100]] });              // one older daily point only
  const svg = simCurveSVG(data({ mean_reversion: hourly, breakout_daily: daily }));
  const lines = [...svg.matchAll(/points="([^"]+)"/g)].map(m => m[1].split(' '));
  assert.equal(lines.length, 2);
  assert.equal(lines[0][0].split(',')[0], lines[1][0].split(',')[0]);   // both start at the left edge
  assert.match(simCurveSVG(data({ a: account({ curve: [[now, 100]] }), b: account({ curve: [[now, 100]] }) })), /第一根新 K 线后出现/);
});

test('backfilled accounts are labelled and the chart marks where live running starts', () => {
  const started = new Date(now - 30 * 86400e3).toISOString(), live = new Date(now - 1800e3).toISOString();
  const acct = account({ started, live_since: live, curve: [[now - 30 * 86400e3, 100], [now - 3600e3, 103], [now, 104]] });
  const d = data({ mean_reversion: acct });
  assert.deepEqual(simLiveSince(d), { started: Date.parse(started), live: Date.parse(live) });
  assert.match(simBackfillNote(d), /之前为历史补跑/);
  assert.match(simCurveSVG(d), /class="sim-live"/);
  const plain = data({ mean_reversion: account({ started: live, live_since: live }) });
  assert.equal(simLiveSince(plain), null); assert.equal(simBackfillNote(plain), ''); assert.doesNotMatch(simCurveSVG(plain), /sim-live/);
});

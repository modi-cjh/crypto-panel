import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { validLab, labBars, labStats, heroMarkup, strategiesMarkup, positionsMarkup, closedMarkup, versionsMarkup, labCaption, labTime } from '../assets/lab.mjs';

const d = JSON.parse(fs.readFileSync(new URL('./fixtures/lab-account.json', import.meta.url), 'utf8'));

test('replay account from export_panel.py is accepted; broken ones are rejected', () => {
  assert.ok(validLab(d));
  assert.equal(validLab({ ...d, schema: 2 }), false);
  assert.equal(validLab({ ...d, ohlc: [] }), false);
  assert.equal(validLab({ ...d, ohlc: [[1, 2, 3]] }), false);
  assert.equal(validLab(null), false);
});

test('4h rows merge into daily and weekly candles without losing the range', () => {
  const four = labBars(d, '4h'), days = labBars(d, 'day'), weeks = labBars(d, 'week');
  assert.equal(four.length, d.ohlc.length);
  assert.equal(days.length, 4);                       // 24 rows × 4h = 4 UTC days from 2025-06-01 00:00
  assert.equal(days[0].time, Date.parse('2025-06-01T00:00:00Z') / 1000);
  assert.equal(days[0].open, d.ohlc[0][0]); assert.equal(days[0].close, d.ohlc[5][3]);
  assert.equal(days[0].high, Math.max(...d.ohlc.slice(0, 6).map(r => r[1])));
  assert.equal(weeks[0].time, Date.parse('2025-05-26T00:00:00Z') / 1000);   // weeks start on Monday
  assert.equal(weeks.at(-1).close, d.ohlc.at(-1)[3]);
  for (const b of [...days, ...weeks]) assert.ok(b.low <= Math.min(b.open, b.close) && b.high >= Math.max(b.open, b.close));
});

test('stats: return, drawdown from the running peak, last 24h change', () => {
  const s = labStats({ ...d, ohlc: [[1000, 1000, 1000, 1200], [1200, 1200, 600, 600], [600, 900, 600, 900]], bar_hours: 4, positions: [] });
  assert.equal(s.equity, 900); assert.equal(s.pnl, -100); assert.equal(s.peak, 1200); assert.equal(s.maxDd, 50);
});

test('markup: hero metrics, active strategies, positions with opening strategy, closed trades, versions', () => {
  const hero = heroMarkup(d);
  for (const k of ['最大回撤', '日亏停手', '保证金占用', 'USDT']) assert.match(hero, new RegExp(k));
  const act = d.strategies.filter(s => s.active);
  const st = strategiesMarkup(d);
  for (const s of act) assert.match(st, new RegExp(s.id));
  if (d.strategies.some(s => !s.active)) assert.match(st, /已下线的 \d+ 个策略合计/);
  const pos = positionsMarkup(d);
  assert.equal((pos.match(/<details/g) || []).length, d.positions.length);
  assert.match(pos, new RegExp(`开仓策略[\\s\\S]*${d.positions[0].sid}`));
  assert.equal(positionsMarkup({ ...d, positions: [] }), '<p class="empty">当前没有持仓</p>');
  assert.equal((closedMarkup(d, 3).match(/<li/g) || []).length, 3);
  assert.match(versionsMarkup(d), /当前/); assert.match(versionsMarkup(d), /v2/);
  assert.match(labCaption(d), /不下单|没有真实或模拟下单/);
});

test('text from the data file is escaped', () => {
  const bad = { ...d, positions: [{ ...d.positions[0], pair: '<img src=x>' }], strategies: [{ ...d.strategies[0], desc: '<script>', active: true }] };
  assert.doesNotMatch(positionsMarkup(bad), /<img/); assert.doesNotMatch(strategiesMarkup(bad), /<script>/);
});

test('replay times are UTC and shown in UTC+7', () => {
  assert.equal(labTime('2026-11-07T21:00'), labTime('2026-11-07T21:00:00Z'));
  assert.match(labTime('2026-11-07T21:00'), /11\/08 04:00|11-08 04:00/);
  assert.match(labTime('2026-11-09T12'), /11\/09 19:00|11-09 19:00/);
});

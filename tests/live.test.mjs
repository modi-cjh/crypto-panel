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

// ---- status light / closed trades / statistics / chart helpers ----
import { liveLight, LIGHT_STALE, LIVE_TRADES_URL, validLiveTrades, closedTradesMarkup, tradeStatsMarkup, curveDrawdown, holdText, exitLabel,
  chartSubject, candleRequest, levelsLadderSVG, tradeMarkers } from '../assets/live.mjs';

const TRADE = { inst: 'SOL-USDT-SWAP', side: 'short', contracts: 1.58, leverage: 10, entry: 72.46, exit: 48.2, stop: 80.18, target: 48.09,
  pnl: 18.4, pnl_pct: 33.48, roe_pct: 334.8, r: 1.508, opened_at: '2026-11-15T14:00:18Z', closed_at: '2026-11-15T17:00:00Z', hold_minutes: 180,
  exit_type: 'tp', outcome: 'target', strategy: 'evolved', champion: 's15653', leader: true };
const tradesDoc = (trades = [TRADE], stats) => ({ schema_version: 1, source: 'okx-live', updated_at: '2026-11-15T17:02:00Z', trades, stats: stats ?? {
  overall: { trades: 3, wins: 2, losses: 1, win_rate: 66.67, pnl: 13.1, avg_win: 9.8, avg_loss: 6.5, payoff: 1.5077, profit_factor: 3.015, avg_r: 0.42, tp: 1, sl: 1, max_drawdown_closed: 6.5 },
  by_strategy: [{ key: 's15653', trades: 2, wins: 2, losses: 0, win_rate: 100, pnl: 19.6, payoff: null, avg_r: 0.9 }, { key: 's20001', trades: 1, wins: 0, losses: 1, win_rate: 0, pnl: -6.5, avg_r: -1.07 }],
  equity_drawdown: { usdt: 30, pct: 25 } } });

test('status light: ok / paused / halted / stale (2 h) / waiting', () => {
  assert.equal(LIGHT_STALE, 2 * 3600000);
  assert.equal(liveLight(null).state, 'wait');
  assert.equal(liveLight(snap(), NOW).state, 'ok');
  assert.equal(liveLight(snap({ paused: true }), NOW).state, 'paused');
  assert.equal(liveLight(snap({ halted: true, paused: true }), NOW).state, 'halted');
  const updated = Date.parse('2026-11-15T14:02:46Z');
  assert.equal(liveLight(snap({ halted: true }), updated + 2 * 3600000 + 1000).state, 'stale');     // stale wins
  assert.equal(liveLight(snap(), updated + 2 * 3600000 - 1000).state, 'ok');
  assert.match(liveLight(snap({ halted: true }), NOW).label, /熔断/);
});

test('trades.json: url, validation, rows, escaping', () => {
  assert.equal(LIVE_TRADES_URL, 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/live-data/trades.json');
  assert.ok(validLiveTrades(tradesDoc()));
  assert.ok(!validLiveTrades({ trades: [{ inst: 'bad', closed_at: 'x' }] }));
  assert.ok(!validLiveTrades(null));
  const html = closedTradesMarkup(tradesDoc());
  for (const s of ['SOL', '空 10×', '72.46', '48.2', '+18.40 U', '+334.80%', '止盈', '3 小时', 's15653', '第一名', 'data-trade="0"']) assert.ok(html.includes(s), s);
  assert.match(closedTradesMarkup(tradesDoc([])), /还没有已平仓/);
  const evil = closedTradesMarkup(tradesDoc([{ ...TRADE, champion: '<img onerror=x>', strategy: '<b>' }]));
  assert.ok(!evil.includes('<img onerror') && evil.includes('&lt;img onerror=x&gt;'));
  const many = closedTradesMarkup(tradesDoc(Array.from({ length: 25 }, () => TRADE)), { limit: 20 });
  assert.match(many, /更早的 5 笔/);
  assert.ok(many.includes('data-trade="24"'));
});

test('exit labels and hold time', () => {
  assert.equal(exitLabel({ exit_type: 'sl' }).short, '止损');
  assert.equal(exitLabel({ exit_type: 'other', reason: 'daily_halt' }).label, '日亏停手平仓');
  assert.equal(exitLabel({}).short, '交易所平仓');
  assert.equal(holdText(45), '45 分钟');
  assert.equal(holdText(185), '3 小时 5 分');
  assert.equal(holdText(3 * 1440 + 120), '3 天 2 小时');
  assert.equal(holdText(undefined), '—');
});

test('statistics panel and drawdown fallback', () => {
  const html = tradeStatsMarkup(tradesDoc(), snap());
  for (const s of ['3 笔', '66.7%', '1.51', '+0.42 R', '+13.10 U', '25.00%', 's20001', '-6.50']) assert.ok(html.includes(s), s);
  // No trades.json yet (older sync): drawdown from the snapshot equity curve, no crash.
  const bare = tradeStatsMarkup({ trades: [] }, snap({ history: [[1, 100], [2, 120], [3, 90], [4, 130]] }));
  assert.ok(bare.includes('25.00%'));
  assert.match(bare, /第一笔平仓后/);
  assert.deepEqual(curveDrawdown([[1, 100], [2, 80]]), { usdt: 20, pct: 20 });
  assert.equal(curveDrawdown([]), null);
});

test('chart subject, candle request window, markers and the fallback ladder', () => {
  const pos = liveView(snap(), new Map(), NOW).positions[0];
  const s = chartSubject({ ...pos, opened_at: '2026-11-15T14:00:18Z' });
  assert.deepEqual(s.levels.map(l => l.role), ['entry', 'stop', 'target']);
  assert.equal(s.side, -1);
  const r = candleRequest(s, NOW);
  assert.equal(r.path, 'market/candles');
  assert.equal(r.params.instId, 'SOL-USDT-SWAP');
  assert.equal(r.bar, '15m');
  const t = chartSubject(TRADE, 'trade');
  assert.deepEqual(t.levels.map(l => l.role), ['entry', 'stop', 'target', 'exit']);
  const old = candleRequest(t, Date.parse('2027-03-01T00:00:00Z'));          // months later: history endpoint ending after the close
  assert.equal(old.path, 'market/history-candles');
  assert.ok(Number(old.params.after) > Date.parse(TRADE.closed_at));
  const bars = [0, 1, 2, 3, 4].map(i => ({ time: Date.parse('2026-11-15T14:00:00Z') / 1000 + i * 3600 }));
  const m = tradeMarkers(t, bars);
  assert.deepEqual(m.map(x => x.text), ['入场', '出场']);
  assert.equal(m[1].time, bars[3].time);
  assert.match(levelsLadderSVG(t), /止损 80\.18/);
  assert.equal(chartSubject({ inst: 'x' }), null);
});

test('position rows: chart button and the dense entry / stop / target line', () => {
  const html = livePositionsMarkup(snap(), liveView(snap(), new Map(), NOW));
  assert.ok(html.includes('data-chart-inst="SOL-USDT-SWAP"'));
  assert.match(html, /入 72\.43.*损 80\.18.*盈 48\.087/);
});

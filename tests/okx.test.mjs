import test from 'node:test';
import assert from 'node:assert/strict';
import { validOkx, okxLive, okxTotals, okxStatus, okxCurveSVG, okxPositionsMarkup, okxActionsMarkup, okxMascotState, okxChanges, okxFeed, okxStart, demoQuoteState, accountDataNote } from '../assets/okx.mjs';

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

test('floating PnL follows demo-market mark prices with the contract value, else the recorded values', () => {
  const marks = (inst, price, at) => new Map([[inst, { price, at }]]);
  const live = okxLive(summary(), marks('BTC-USDT-SWAP', 70500, NOW - 1000), NOW);
  assert.ok(live.live); assert.equal(live.positions[0].upl, 15);            // 3 contracts × 0.01 BTC × 500
  assert.equal(live.equity, 5030 - 3 + 15);
  const stale = okxLive(summary(), marks('BTC-USDT-SWAP', 70500, NOW - 60000), NOW);
  assert.equal(stale.live, false); assert.equal(stale.equity, 5030); assert.equal(stale.positions[0].upl, 3);
  const short = okxLive(summary({ positions: [{ inst: 'ETH-USDT-SWAP', side: 'net', contracts: '-2', avgPx: '3000', upl: '0', ctVal: 0.1, lever: '5' }] }), marks('ETH-USDT-SWAP', 2900, NOW), NOW);
  assert.equal(short.positions[0].side, -1); assert.equal(short.positions[0].upl, 20);
});

test('totals report return against the starting equity and today against the day start', () => {
  const t = okxTotals(summary(), new Map(), NOW);
  assert.equal(t.ret.toFixed(2), '0.60'); assert.equal(t.day, 30); assert.equal(t.positions, 1); assert.equal(t.trades, 2);
  assert.equal(okxStart(summary({ start_equity: undefined })), 5000);
  assert.equal(okxStart(summary({ start_equity: undefined, history: [] })), 5000);
});

test('trading switch and delayed account snapshots are distinct', () => {
  assert.equal(okxStatus(summary({updated_at:new Date(NOW-300e3).toISOString()}), NOW).label, '允许开仓');
  assert.equal(okxStatus(summary(), NOW).label, '允许开仓');                                   // 20 minutes old: runs are 4 hours apart
  assert.equal(okxStatus(summary(), NOW + 4.5 * 3600e3).label, '允许开仓');
  assert.equal(okxStatus(summary({ paused: true }), NOW).label, '已暂停');
  assert.equal(okxStatus(summary({ halted: true, paused: true }), NOW).label, '日亏停手');
  assert.equal(okxStatus(summary(), NOW + 6 * 3600e3).label, '快照延迟 · 未暂停');
  assert.equal(okxStatus(summary({paused:true}), NOW + 6 * 3600e3).label, '已暂停');
});

test('demo quote connectivity requires fresh marks for every held instrument',()=>{
  const data=summary(),marks=new Map([['BTC-USDT-SWAP',{price:70100,at:NOW}]]);
  assert.equal(demoQuoteState(data,new Map(),{state:'实时',lastData:NOW},NOW,NOW).label,'行情断开');
  assert.equal(demoQuoteState(data,marks,{state:'实时',lastData:NOW},0,NOW).label,'行情实时');
  assert.equal(demoQuoteState(data,marks,{state:'离线',lastData:0},NOW,NOW).label,'行情轮询');
  assert.equal(demoQuoteState(data,marks,{state:'实时',lastData:NOW},NOW,NOW+31000).label,'行情断开');
  assert.equal(demoQuoteState({...data,positions:[]},new Map(),null,0,NOW).label,'账户快照');
  const note=accountDataNote(data,demoQuoteState(data,new Map(),null,0,NOW),NOW);
  assert.match(note,/距今 20 分钟/);assert.match(note,/使用上次快照/);assert.doesNotMatch(note,/更新延迟/);
  assert.match(accountDataNote(data,demoQuoteState(data,new Map(),null,0,NOW+6*3600e3),NOW+6*3600e3),/更新延迟/);
  assert.match(accountDataNote(data,demoQuoteState(data,marks,{state:'实时',lastData:NOW},0,NOW),NOW),/按模拟行情估值/);
});

test('curve draws the equity history and waits for enough points', () => {
  assert.match(okxCurveSVG(summary()), /<polyline/);
  assert.match(okxCurveSVG(summary({ history: [[NOW, 5000]] })), /资金曲线会在/);
});

test('positions and actions are labelled in Chinese and exchange text is escaped', () => {
  const pos = okxPositionsMarkup(summary(), new Map(), NOW);
  assert.match(pos, /趋势突破只做多/); assert.match(pos, /69,000</); assert.match(pos, /\+3\.00 USDT/);
  assert.match(okxPositionsMarkup(summary({ positions: [{ ...summary().positions[0], ours: false }] }), new Map(), NOW), /非本程序仓位/);
  assert.match(okxPositionsMarkup(summary({ positions: [] })), /当前没有持仓/);
  const acts = okxActionsMarkup(summary({ recent_actions: [...summary().recent_actions, { ts: '2026-11-03T07:00:00Z', action: 'error', inst: '-', detail: '<b>bad</b>' }] }));
  assert.match(acts, /信号太旧，价格已离开入场区间/); assert.match(acts, /多 3 张 @ 70000/); assert.match(acts, /&lt;b&gt;bad/); assert.doesNotMatch(acts, /<b>bad/);
});

test('confirmed execution-test fills show their source, entry, exit and net costs', () => {
  const shared = { ts: '2026-11-03T21:51:06Z', inst: 'BTC-USDT-SWAP', execution_test: true, strategy: 'execution_test', side: 1, contracts: '0.01', leverage: 1 };
  const html = okxActionsMarkup(summary({ recent_actions: [
    { ...shared, action: 'close', reason: 'execution_test', price: 77876.31, net_pnl: -.0077966355 },
    { ...shared, action: 'open', price: 77876.4, stop: '77097.64' },
  ] }));
  assert.match(html, /开仓 · BTC/);
  assert.match(html, /平仓 · BTC/);
  assert.match(html, /模拟成交测试/);
  assert.match(html, /0.01 张 @ 77876.31/);
  assert.match(html, /-0.0078 USDT（含手续费）/);
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

test('companion uses the latest trade instead of announcing a position already closed', () => {
  const opened = {ts:'2026-11-03T11:50:00Z',action:'open',inst:'BTC-USDT-SWAP',side:1};
  const closed = {ts:'2026-11-03T11:51:00Z',action:'close',inst:'BTC-USDT-SWAP',execution_test:true,reason:'execution_test'};
  let state = okxMascotState(summary({recent_actions:[closed,opened]}), NOW);
  assert.equal(state.label, '测试完成');
  assert.match(state.message, /BTC.*已平仓/);
  state = okxMascotState(summary({recent_actions:[{...closed,execution_test:false,reason:'paper_closed'},opened]}), NOW);
  assert.equal(state.label, '刚平仓');
  state = okxMascotState(summary({recent_actions:[{...opened,ts:'2026-11-03T11:55:00Z'},{...closed,action:'closed_on_exchange'}]}), NOW);
  assert.equal(state.label, '刚开仓');
});

test('exchange-side closes show the realised amount, price and which order filled', async () => {
  const { okxActionsMarkup: acts, okxFeed: feed } = await import('../assets/okx.mjs');
  const d = { recent_actions: [{ ts: '2026-11-04T01:33:13Z', action: 'closed_on_exchange', inst: 'XRP-USDT-SWAP', pnl: 5.9, close_price: 1.2556, outcome: 'target' },
    { ts: '2026-11-04T01:00:00Z', action: 'closed_on_exchange', inst: 'BTC-USDT-SWAP', pnl: -50.8, close_price: 68610, outcome: 'stop' },
    { ts: '2026-11-04T00:30:00Z', action: 'closed_on_exchange', inst: 'ETH-USDT-SWAP' }] };
  const html = acts(d, 10, { kind: 'trades' });
  assert.match(html, /交易所止盈触发 · XRP/); assert.match(html, /成交价 1\.2556 · 已实现 \+5\.90 USDT（含手续费）/);
  assert.match(html, /交易所止损触发 · BTC/); assert.match(html, /已实现 -50\.80 USDT/);
  assert.match(html, /交易所止损 \/ 止盈触发 · ETH[\s\S]*金额待下次运行从交易所补全/);
  assert.match(html, /data-tone="close"[\s\S]*XRP/);
  assert.match(feed(d)[0].text, /XRP 交易所止盈触发 · \+5\.90 USDT/);
});

test('a target fill that lost money explains the price, fee and funding parts', async () => {
  const { okxActionsMarkup: acts } = await import('../assets/okx.mjs');
  const html = acts({ recent_actions: [{ ts: '2026-11-04T01:33:13Z', action: 'closed_on_exchange', inst: 'XRP-USDT-SWAP', pnl: -11.28, price_pnl: 5.69, fee: -1.99, funding: -14.98, close_price: 1.2559, outcome: 'target' }] }, 10, { kind: 'trades' });
  assert.match(html, /交易所止盈触发 · XRP/);
  assert.match(html, /已实现 -11\.28 USDT（价差 \+5\.69，手续费 -1\.99，资金费 -14\.98）/);
});

test('daily halt notice shows the actual loss against the 10 % line', async () => {
  const { okxActionsMarkup: acts } = await import('../assets/okx.mjs');
  const html = acts({ recent_actions: [{ ts: '2026-11-05T08:00:00Z', action: 'daily_halt', inst: '-', equity: 4495, day_start: 5000 }] });
  assert.match(html, /当日亏损 10\.1%达到停手线（10%）/);
});

test('headline return names one outsized trade, and the mirror is compared with the paper book', async () => {
  const { okxOutlier, okxOutlierNote, mirrorCompareMarkup } = await import('../assets/okx.mjs');
  const d = { start_equity: 5000, history: [], positions: [{ inst: 'SOL-USDT-SWAP', side: 'short', ours: true }, { inst: 'ICP-USDT-SWAP', side: 'long', ours: true }],
    recent_actions: [{ action: 'skip', inst: 'BTC-USDT-SWAP', reason: 'paper_entry_too_old' }], largest_close: { ts: '2026-11-10T16:37:46Z', inst: 'XRP-USDT-SWAP', pnl: 2878.8 } };
  const o = okxOutlier(d, { equity: 7337.03 });
  assert.equal(o.inst, 'XRP-USDT-SWAP'); assert.ok(Math.abs(o.retWithout - (-10.84)) < .01);
  assert.match(okxOutlierNote(o), /其中 \+2,878\.80 USDT 来自[\s\S]*XRP[\s\S]*扣除后[\s\S]*-10\.84%/);
  assert.equal(okxOutlier({ ...d, largest_close: { ts: d.largest_close.ts, inst: 'XRP-USDT-SWAP', pnl: 300 } }, { equity: 5300 }), null);   // an ordinary trade: no note
  assert.equal(okxOutlierNote(null), '');
  const evo = { paper: { positions: [{ inst: 'SOL-USDT-SWAP', side: -1 }, { inst: 'BTC-USDT-SWAP', side: -1 }] } };
  const html = mirrorCompareMarkup(d, evo);
  assert.match(html, /纸面持仓 <b>2<\/b> 个，OKX 跟上 <b>1<\/b> 个，另有 1 个等待平仓/);
  assert.match(html, /SOL[\s\S]*已跟上/); assert.match(html, /BTC[\s\S]*没跟上[\s\S]*信号太旧/); assert.match(html, /ICP[\s\S]*纸面已平/);
  assert.match(mirrorCompareMarkup(d, null), /等待纸面账户/);
});

test('positions name the strategy that opened them; stop moves are rounded and listed under the run log', async () => {
  const { okxPositionsMarkup: pm, okxActionsMarkup: am } = await import('../assets/okx.mjs');
  const d = summary({ positions: [{ inst: 'CRV-USDT-SWAP', side: -1, ours: true, contracts: '7120', avgPx: '0.2419', markPx: '0.24', upl: '-1.1', lever: '7', margin: '245.9', ctVal: 1 }],
    open_mirrors: [{ inst: 'CRV-USDT-SWAP', strategy: 'evolved', champion: 's13215', leader: true, stop: .2662 }],
    recent_actions: [{ ts: '2026-11-03T11:39:00Z', action: 'amend_stop', inst: 'GRASS-USDT-SWAP', old: 0.5095269058416524, new: 0.5035269058416524 }] });
  assert.match(pm(d), /s13215 · <b>第一名<\/b>/);
  const old = { ...d, open_mirrors: [{ inst: 'CRV-USDT-SWAP', strategy: 'evolved', stop: .2662 }] };              // opened before the mirror stored the strategy
  assert.match(pm(old), /进化冠军/); assert.match(pm(old, new Map(), NOW, { owners: { 'CRV-USDT-SWAP': { champion: 's12821', boost: false } } }), /okx-pos-tag">s12821</);
  assert.doesNotMatch(pm(d), /<details[^>]* open/);                                   // collapsed until tapped
  assert.equal(am(d, 10, { kind: 'trades' }).includes('GRASS'), false);
  const log = am(d, 10, { kind: 'system' });
  assert.match(log, /止损 0\.50953 → 0\.50353/); assert.doesNotMatch(log, /0\.5095269/);
});

test('daily returns: each UTC+7 day against the previous day, today at the live equity, gaps named', async () => {
  const { okxDailyReturns: dr, okxDailyMarkup: dm } = await import('../assets/okx.mjs');
  const at = (day, hour) => Date.parse(`2026-11-${day}T${String(hour).padStart(2, '0')}:00:00+07:00`);
  const d = { history: [[at('08', 9), 5000], [at('08', 21), 5100], [at('09', 9), 4998], [at('12', 8), 6000]] };
  const rows = dr(d, 6060, at('12', 15));
  assert.deepEqual(rows.map(r => r.day), ['2026-11-12', '2026-11-09', '2026-11-08']);
  assert.ok(Math.abs(rows[2].pct - 2) < 1e-9);                                  // 5000 → 5100
  assert.ok(Math.abs(rows[1].pct - (4998 / 5100 - 1) * 100) < 1e-9);
  assert.ok(Math.abs(rows[0].pct - (6060 / 4998 - 1) * 100) < 1e-9); assert.equal(rows[0].gap, 2);   // 11/10 and 11/11 had no run
  const html = dm(rows);
  assert.match(html, /今天<small>含前 2 天<\/small>/); assert.match(html, /11\/08/); assert.match(html, /\+2\.00%/);
  assert.match(html, /okx-daily-bar down/);
  assert.match(dm([]), /跑满一天后/);
  const withStart = dr({ ...d, day_start: 5000 }, 6060, at('12', 15));
  assert.ok(Math.abs(withStart[0].pct - 21.2) < 1e-9);                           // today against day_start, matching the header
});

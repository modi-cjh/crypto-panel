// OKX live account (real money). Two sources with the same fields:
//   1. the server's read-only API (tools/live_readapi.py behind Caddy): /live.json = {snapshot, fills, trades}, refreshed every minute
//   2. the live-data branch, published hourly by tools/live_sync.py (fallback when the API cannot be reached):
//   snapshot.json  equity / available / day PnL / paused / halted / positions with stop and target
//   fills.json     recent opens / closes and a short list of skips / pauses
//   trades.json    closed trades (entry / exit / PnL / hold time / exit type / seed) and statistics (optional, newer syncs)
// Pure functions only (rendering to HTML strings); the page wiring lives in app.mjs.
import { coinIcon } from './coins.mjs';

export const LIVE_BASE = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/live-data/';
export const LIVE_SNAPSHOT_URL = LIVE_BASE + 'snapshot.json';
export const LIVE_FILLS_URL = LIVE_BASE + 'fills.json';
// Server read-only API (GET only, whitelisted fields, no keys). tickers.json = OKX public tickers cached by the server,
// the market fallback when the browser cannot reach OKX directly (some 4G networks block it).
export const LIVE_API_BASE = 'https://139-180-146-42.sslip.io/';
export const LIVE_API_URL = LIVE_API_BASE + 'live.json';
export const LIVE_TICKERS_URL = LIVE_API_BASE + 'tickers.json';
export const API_FRESH = 5 * 60000;    // an API document older than this is no better than live-data: say so
// The trader runs hourly on the server and the box syncs hourly; raw.githubusercontent caches ~5 minutes.
export const LIVE_STALE = 2.5 * 3600000;
export const LIVE_COLOR = '#06727d';
const MARK_FRESH = 30000;

const finite = Number.isFinite;
const num = v => v === null || v === undefined || v === '' ? NaN : Number(v);
const isInst = s => typeof s === 'string' && /^[A-Z0-9]+-USDT-SWAP$/.test(s);
const stamp = t => typeof t === 'string' && finite(Date.parse(t));
export const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = (v, d = 2) => finite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—';
export const signed = (v, d = 2) => finite(v) ? (v > 0 ? '+' : '') + fmt(v, d) : '—';
export const tone = v => finite(v) ? v >= 0 ? 'up' : 'down' : '';
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Phnom_Penh', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
export const time = t => { const ms = typeof t === 'number' ? t : Date.parse(t); return finite(ms) ? clock.format(ms) : '—'; };
// Prices at exchange precision: about five significant digits, no padding zeros.
export const px = v => finite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: Math.min(8, Math.max(2, 4 - Math.floor(Math.log10(Math.abs(v) || 1)))) }) : '—';
const coin = inst => String(inst ?? '').split('-')[0];
const sideNum = s => s === 'long' || s === 1 ? 1 : s === 'short' || s === -1 ? -1 : 0;

export function validLiveSnapshot(d) {
  return d !== null && typeof d === 'object' && d.trading_mode === 'live' && stamp(d.updated_at) && finite(d.equity) &&
    Array.isArray(d.positions) && d.positions.every(p => p && isInst(p.inst) && sideNum(p.side) !== 0) &&
    (d.history === undefined || Array.isArray(d.history) && d.history.every(p => Array.isArray(p) && finite(p[0]) && finite(p[1])));
}
export function validLiveFills(d) {
  const row = r => r && typeof r === 'object' && stamp(r.ts) && typeof r.action === 'string';
  return d !== null && typeof d === 'object' && Array.isArray(d.fills) && d.fills.every(row) && (d.events === undefined || Array.isArray(d.events) && d.events.every(row));
}

// Floating PnL from fresh public mark prices (marks: Map inst -> {price, at}); otherwise the synced values.
export function liveView(d, marks = new Map(), now = Date.now()) {
  if (!d) return null;
  const positions = d.positions.map(p => {
    const side = sideNum(p.side), m = marks.get(p.inst), mark = m && now - m.at >= 0 && now - m.at <= MARK_FRESH ? m.price : 0;
    const qty = Math.abs(num(p.contracts)), ct = num(p.ctVal) > 0 ? num(p.ctVal) : NaN, entry = num(p.avgPx);
    if (mark > 0 && ct > 0 && qty > 0 && entry > 0) return { ...p, side, mark, upl: side * qty * ct * (mark - entry), live: true };
    return { ...p, side, mark: num(p.markPx), upl: num(p.upl), live: false };
  });
  const recorded = d.positions.reduce((s, p) => s + (finite(num(p.upl)) ? num(p.upl) : 0), 0);
  const live = positions.length > 0 && positions.every(p => p.live);
  const equity = live ? d.equity - recorded + positions.reduce((s, p) => s + p.upl, 0) : d.equity;
  const dayStart = num(d.day_start), start = num(d.start_equity);
  const day = dayStart > 0 ? equity - dayStart : null;
  return {
    equity, available: num(d.available), live, positions,
    day, dayPct: day === null ? null : day / dayStart * 100,
    total: start > 0 ? equity - start : null, totalPct: start > 0 ? (equity / start - 1) * 100 : null,
    upl: positions.reduce((s, p) => s + (finite(p.upl) ? p.upl : 0), 0),
    margin: positions.reduce((s, p) => s + (finite(num(p.margin)) ? num(p.margin) : 0), 0),
  };
}

export function liveFreshness(d, now = Date.now()) {
  if (!d) return { text: '等待实盘快照', stale: true, minutes: null };
  const age = Math.max(0, now - Date.parse(d.updated_at)), mins = Math.floor(age / 60000);
  const ago = mins < 1 ? '刚刚' : mins < 90 ? `${mins} 分钟前` : `${Math.round(mins / 60)} 小时前`;
  const stale = age > LIVE_STALE;
  return { text: `更新 ${time(d.updated_at)}（金边时间 UTC+7）· ${ago}${stale ? ' · 超过 2.5 小时未更新，可能离线' : ''}`, stale, minutes: mins };
}

export function liveStatus(d, now = Date.now()) {
  if (!d) return { label: '等待数据', cls: 'muted' };
  if (now - Date.parse(d.updated_at) > LIVE_STALE) return { label: '数据离线', cls: 'down' };
  if (d.halted) return { label: '日亏停手', cls: 'down' };
  if (d.paused) return { label: '已暂停', cls: 'muted' };
  if (d.dry_run) return { label: '空跑 · 不下单', cls: 'muted' };
  return { label: '实盘运行中', cls: 'up' };
}

export function liveCurveSVG(d) {
  const pts = (d?.history || []).slice().sort((a, b) => a[0] - b[0]);
  if (pts.length < 2 || pts.at(-1)[0] === pts[0][0]) return '<p class="empty">资金曲线会在实盘运行几个小时后出现（每次运行一个点）</p>';
  const base = num(d.start_equity) > 0 ? num(d.start_equity) : pts[0][1], t0 = pts[0][0], t1 = pts.at(-1)[0], vals = pts.map(p => p[1]);
  const lo = Math.min(...vals, base), hi = Math.max(...vals, base), pad = Math.max((hi - lo) * .12, base * .002);
  const W = 680, H = 250, L = 118, R = 12, T = 16, B = 40, x = t => L + (t - t0) / (t1 - t0) * (W - L - R), y = v => T + (hi + pad - v) / (hi - lo + pad * 2) * (H - T - B);
  const grid = [hi + pad, base, lo - pad].map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === base ? 'sim-base' : 'equity-grid'}"/><text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${fmt(v)}</text>`).join('');
  const dir = vals.at(-1) >= vals[0] ? 'curve-up' : 'curve-down', line = pts.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ');
  const floor = (H - B).toFixed(1), tip = `<circle class="curve-tip ${dir}" cx="${x(t1).toFixed(1)}" cy="${y(vals.at(-1)).toFixed(1)}" r="4.5"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="OKX 实盘账户资金曲线">${grid}<polygon class="curve-area ${dir}" points="${x(t0).toFixed(1)},${floor} ${line} ${x(t1).toFixed(1)},${floor}"/>` +
    `<polyline class="curve-line ${dir}" points="${line}" fill="none" stroke="${LIVE_COLOR}" stroke-width="2" vector-effect="non-scaling-stroke"/>${tip}` +
    `<text x="${L}" y="${H - 8}">${time(t0)}</text><text x="${W - R}" y="${H - 8}" text-anchor="end">${time(t1)}</text></svg>`;
}

const MODES = { cross: '全仓', isolated: '逐仓' };
const STRATEGIES = { evolved: '进化冠军' };
// One row per position: coin, side, leverage and floating PnL visible; tap for the details.
export function livePositionsMarkup(d, view, { open = false } = {}) {
  if (!d || !view) return '<p class="skeleton" role="status">数据加载中</p>';
  if (!view.positions.length) return '<p class="empty">当前没有持仓，等待策略信号</p>';
  return view.positions.map(p => {
    const margin = num(p.margin), pct = margin > 0 && finite(p.upl) ? p.upl / margin * 100 : null;
    const tag = !p.ours ? '人工仓位（程序不管理）' : p.champion ? `${esc(p.champion)}${p.leader ? ' · <b>第一名</b>' : ''}` : esc(STRATEGIES[p.strategy] || '策略仓位');
    // One dense line under the seed: entry · stop · target (shown on phones, where the details take a tap).
    const line = [['入', num(p.avgPx), ''], ['损', num(p.stop), 'down'], ['盈', num(p.target), 'up']].filter(([, v]) => finite(v))
      .map(([k, v, c]) => `<span class="${c}">${k} ${px(v)}</span>`).join(' · ');
    const cells = [
      ['张数', finite(num(p.contracts)) ? esc(Math.abs(num(p.contracts))) : '—'],
      ['杠杆 / 模式', `${esc(finite(num(p.lever)) ? num(p.lever) : '—')}× · ${esc(MODES[p.mgnMode] || p.mgnMode || '—')}`],
      ['开仓均价', px(num(p.avgPx))],
      [p.live ? '标记价（实时）' : `标记价（${time(d.updated_at)}）`, px(p.mark)],
      ['止损', finite(num(p.stop)) ? `<span class="down">${px(num(p.stop))}</span>` : '—'],
      ['止盈', finite(num(p.target)) ? `<span class="up">${px(num(p.target))}</span>` : '—'],
      ['保证金 USDT', fmt(margin)],
      ['浮盈 USDT', finite(p.upl) ? signed(p.upl, 4) : '—', tone(p.upl)],
      ['收益率（按保证金）', pct === null ? '—' : `${signed(pct)}%`, tone(p.upl)],
    ];
    return `<details class="okx-pos sim-position${p.ours ? '' : ' manual'}" data-inst="${esc(p.inst)}"${open ? ' open' : ''}><summary>` +
      `<span class="okx-pos-coin">${coinIcon(p.inst)}<b>${esc(coin(p.inst))}</b><i class="okx-side ${p.side === 1 ? 'up' : 'down'}">${p.side === 1 ? '多' : '空'} ${esc(finite(num(p.lever)) ? num(p.lever) : '—')}×</i></span>` +
      `<span class="okx-pos-tag"><span>${tag}</span><small class="okx-pos-line">${line}</small></span>` +
      `<span class="okx-pos-pnl ${tone(p.upl)}"><b>${finite(p.upl) ? `${signed(p.upl)} USDT` : '—'}</b><small>${pct === null ? '' : `${signed(pct)}%`}</small></span>` +
      `<button type="button" class="okx-pos-chart" data-chart-inst="${esc(p.inst)}" aria-label="${esc(coin(p.inst))} K 线（入场 / 止损 / 止盈）" title="看 K 线"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3v3H4v9h2v6h2v-6h2V6H8V3zm10 2v4h-2v8h2v4h2v-4h2V9h-2V5z"/></svg></button></summary>` +
      `<div class="history-metrics okx-pos-body">${cells.map(([k, v, c = '']) => `<div><span>${k}</span><strong class="${c}">${v}</strong></div>`).join('')}</div></details>`;
  }).join('');
}

const ACTIONS = { open: '开仓', close: '平仓', closed_on_exchange: '交易所止损 / 止盈触发', amend_stop: '上移止损', restore_stop: '补挂止损',
  daily_halt: '日亏停手', flatten: '全部平仓并暂停', skip: '跳过信号', pause: '暂停开仓', resume: '恢复开仓', error: '出错' };
const OUTCOME = { stop: '交易所止损触发', target: '交易所止盈触发', liquidation: '强平' };
const REASONS = { paper_entry_too_old: '信号太旧，价格已离开入场区间', stop_too_close_or_crossed: '价格已接近或越过止损', target_already_reached: '价格已到止盈',
  size_below_minimum: '仓位小于最小下单量', insufficient_margin: '保证金不足', exchange_position_exists_not_ours: '交易所已有人工仓位', no_price: '取不到价格',
  no_instrument_spec: '取不到合约信息', no_leverage_spec: '取不到杠杆上限', same_side_risk: '达到同向风险上限', same_side_count: '同方向持仓数已达上限',
  existing_risk_unknown: '现有仓位风险暂时无法确认', stop_before_liquidation: '止损前会触及强平线', paper_closed: '策略信号平仓', daily_halt: '日亏停手平仓',
  manual_flatten: '手动全部平仓', stop_missing_price_beyond_stop: '止损单缺失且价格已越过止损', signals_stale: '信号过期' };
const titleOf = a => a.action === 'closed_on_exchange' && OUTCOME[a.outcome] ? OUTCOME[a.outcome] : ACTIONS[a.action] || a.action;
const toneOf = a => (a.action === 'close' || a.action === 'closed_on_exchange') && finite(num(a.pnl)) ? (num(a.pnl) >= 0 ? 'close' : 'stop')
  : { open: 'open', close: 'close', closed_on_exchange: 'stop', daily_halt: 'stop', error: 'stop', flatten: 'stop' }[a.action] || 'info';
export function fillDetail(a) {
  const side = sideNum(a.side) === 1 ? '多' : sideNum(a.side) === -1 ? '空' : '';
  const parts = [];
  if (a.action === 'open') parts.push(`${side} ${a.contracts ?? '—'} 张 @ ${px(num(a.price))}`, finite(num(a.stop)) ? `止损 ${px(num(a.stop))}` : '', finite(num(a.target)) ? `止盈 ${px(num(a.target))}` : '', finite(num(a.leverage)) ? `${num(a.leverage)}×` : '');
  else if (a.action === 'skip' || a.action === 'close') parts.push(REASONS[a.reason] || a.reason || '');
  if (a.action === 'close' || a.action === 'closed_on_exchange') parts.push(finite(num(a.close_price)) ? `成交价 ${px(num(a.close_price))}` : '', finite(num(a.pnl)) ? `已实现 ${signed(num(a.pnl))} USDT（含手续费）` : '');
  if (a.action === 'amend_stop' || a.action === 'restore_stop') parts.push(finite(num(a.stop)) ? `止损 ${px(num(a.stop))}` : '');
  return parts.filter(Boolean).join(' · ');
}
export function liveFillsMarkup(rows, limit = 15, empty = '还没有成交，等待策略信号') {
  const list = (rows || []).slice(0, limit);
  if (!list.length) return `<li class="empty">${esc(empty)}</li>`;
  return list.map(a => { const detail = fillDetail(a);
    return `<li class="okx-act" data-tone="${toneOf(a)}"><div><span><i class="okx-act-dot" aria-hidden="true"></i><b>${esc(titleOf(a))}${isInst(a.inst) ? ' · ' + esc(coin(a.inst)) : ''}</b>${a.dry_run ? ' <small class="muted">(空跑)</small>' : ''}</span><time>${esc(time(a.ts))}</time></div>${detail ? `<p>${esc(detail)}</p>` : ''}</li>`;
  }).join('');
}

// ---- companion (mascot) -------------------------------------------------------------------------
const fillKey = a => `${a.ts}|${a.action}|${a.inst}`;
// Fills that appeared since the previous fills.json, oldest first, shaped for robot-work.tradeBurst.
export function liveNewFills(prev, next) {
  if (!prev || !next) return [];
  const seen = new Set((prev.fills || []).map(fillKey));
  return (next.fills || []).filter(a => !seen.has(fillKey(a))).reverse().map(a => ({ ...a, side: sideNum(a.side) }));
}
export function liveFeed(fills, limit = 20) {
  return (fills?.fills || []).map(a => ({ ts: Date.parse(a.ts), text: `${titleOf(a)}${isInst(a.inst) ? ' ' + coin(a.inst) : ''}${fillDetail(a) ? ' · ' + fillDetail(a) : ''}`,
    mood: toneOf(a) === 'stop' ? 'sad' : 'event' })).filter(r => finite(r.ts)).slice(0, limit);
}
export function liveMascotState(d, fills, now = Date.now()) {
  if (!d || now - Date.parse(d.updated_at) > LIVE_STALE) return { state: 'offline', label: '离线', message: '实盘账户超过 2.5 小时没有更新，我在等下一次同步' };
  if (d.halted) return { state: 'paused', label: '停手', message: '今日触发日亏停手，不再开新仓' };
  if (d.paused) return { state: 'paused', label: '暂停', message: '实盘已暂停开新仓' };
  const latest = (fills?.fills || []).find(a => ['open', 'close', 'closed_on_exchange'].includes(a.action) && now - Date.parse(a.ts) >= 0 && now - Date.parse(a.ts) <= 3 * 3600000);
  if (latest?.action === 'open') return { state: 'excited', label: '刚开仓', message: `刚开仓 ${coin(latest.inst)} ${sideNum(latest.side) === 1 ? '多' : '空'}` };
  if (latest) return { state: finite(num(latest.pnl)) && num(latest.pnl) >= 0 ? 'happy' : 'sad', label: '刚平仓', message: `${coin(latest.inst)} ${titleOf(latest)}` };
  const v = liveView(d, new Map(), now);
  if (v.day > 0) return { state: 'happy', label: '今日盈利', message: `实盘今日 ${signed(v.day)} USDT` };
  return { state: 'idle', label: '待命', message: d.positions.length ? `实盘持仓 ${d.positions.length} 个，止损挂在交易所` : '实盘空仓中，等待新信号' };
}

// ---- status light (header): normal / paused / halted / stale ------------------------------------
// Stale after 2 hours without a new snapshot (the trader runs and the box syncs hourly).
export const LIGHT_STALE = 2 * 3600000;
export function liveLight(d, now = Date.now()) {
  if (!d || !stamp(d.updated_at)) return { state: 'wait', label: '等待数据', title: '正在读取实盘快照' };
  const age = now - Date.parse(d.updated_at), hrs = Math.floor(age / 3600000), mins = Math.max(0, Math.floor(age / 60000));
  const ago = mins < 90 ? `${mins} 分钟前` : `${hrs} 小时前`;
  if (age > LIGHT_STALE) return { state: 'stale', label: '数据过期', title: `超过 2 小时没有更新（最近 ${time(d.updated_at)}，${ago}）` };
  if (d.halted) return { state: 'halted', label: '熔断', title: `当日亏损触发熔断，今天不再开新仓 · 更新 ${ago}` };
  if (d.paused) return { state: 'paused', label: '暂停', title: `实盘已暂停开新仓 · 更新 ${ago}` };
  if (d.dry_run) return { state: 'paused', label: '空跑', title: `空跑模式，不会真实下单 · 更新 ${ago}` };
  return { state: 'ok', label: '运行正常', title: `实盘运行中 · 更新 ${ago}` };
}
export function liveLightMarkup(d, now = Date.now()) {
  const l = liveLight(d, now);
  return `<i class="live-light-dot" aria-hidden="true"></i><b>${esc(l.label)}</b>`;
}

// ---- closed trades + statistics (live-data/trades.json, built box-side by live_sync.build_trades) ----
export const LIVE_TRADES_URL = LIVE_BASE + 'trades.json';
export function validLiveTrades(d) {
  return d !== null && typeof d === 'object' && Array.isArray(d.trades) &&
    d.trades.every(t => t && typeof t === 'object' && isInst(t.inst) && stamp(t.closed_at)) &&
    (d.stats === undefined || d.stats === null || typeof d.stats === 'object');
}
const EXIT = { tp: ['止盈', 'up'], sl: ['止损', 'down'], liq: ['强平', 'down'], other: ['策略平仓', 'muted'], unknown: ['交易所平仓', 'muted'] };
export function exitLabel(t) {
  const [label, cls] = EXIT[t?.exit_type] || EXIT.unknown;
  return { label: t?.exit_type === 'other' && REASONS[t.reason] ? REASONS[t.reason] : label, short: label, cls };
}
export function holdText(min) {
  if (!finite(min) || min < 0) return '—';
  if (min < 60) return `${Math.round(min)} 分钟`;
  if (min < 48 * 60) { const h = Math.floor(min / 60), m = Math.round(min % 60); return m ? `${h} 小时 ${m} 分` : `${h} 小时`; }
  const d = Math.floor(min / 1440), h = Math.round((min % 1440) / 60); return h ? `${d} 天 ${h} 小时` : `${d} 天`;
}
const seedOf = t => t.champion ? esc(t.champion) + (t.leader ? ' · <b>第一名</b>' : '') : esc(STRATEGIES[t.strategy] || t.strategy || '—');
// One row per closed trade (newest first). Tap a row to see its candles.
export function closedTradesMarkup(doc, { limit = 20 } = {}) {
  if (!doc) return '<p class="skeleton" role="status">数据加载中</p>';
  const list = doc.trades || [];
  if (!list.length) return '<p class="empty">还没有已平仓的交易。第一笔平仓（止盈 / 止损 / 策略平仓）后会出现在这里。</p>';
  const row = (t, i) => {
    const side = sideNum(t.side), ex = exitLabel(t), pnl = num(t.pnl), pct = num(t.roe_pct), move = num(t.pnl_pct);
    return `<li class="trade-row" data-trade="${i}" tabindex="0" role="button" aria-label="${esc(coin(t.inst))} 已平仓，查看 K 线">` +
      `<span class="trade-coin">${coinIcon(t.inst)}<b>${esc(coin(t.inst))}</b><i class="okx-side ${side === 1 ? 'up' : 'down'}">${side === 1 ? '多' : side === -1 ? '空' : '—'}${finite(num(t.leverage)) ? ' ' + esc(num(t.leverage)) + '×' : ''}</i></span>` +
      `<span class="trade-px"><small>入场 → 出场</small>${px(num(t.entry))} → ${px(num(t.exit))}</span>` +
      `<span class="trade-pnl ${tone(pnl)}"><b>${finite(pnl) ? signed(pnl) + ' U' : '—'}</b><small>${finite(pct) ? signed(pct) + '%' : finite(move) ? signed(move) + '%' : ''}</small></span>` +
      `<span class="trade-meta"><i class="trade-exit ${ex.cls}" title="${esc(ex.label)}">${esc(ex.short)}</i><span>${esc(holdText(num(t.hold_minutes)))}</span><span class="trade-seed">${seedOf(t)}</span><time>${esc(time(t.closed_at))}</time></span></li>`;
  };
  const head = list.slice(0, limit).map(row).join(''), rest = list.slice(limit);
  return `<ol class="trade-list">${head}</ol>` + (rest.length ? `<details class="trade-more"><summary>更早的 ${rest.length} 笔</summary><ol class="trade-list">${rest.map((t, i) => row(t, i + limit)).join('')}</ol></details>` : '') +
    '<p class="trade-note muted">盈亏 USDT 为交易所已实现盈亏（含手续费与资金费）；% 为按杠杆计的保证金收益率（不含费用）。点一行看 K 线。</p>';
}

// Max drawdown of an equity curve [[ms, equity], ...] (fallback when trades.json has no stats yet).
export function curveDrawdown(history) {
  let peak = null, best = { usdt: 0, pct: 0 };
  for (const [, e] of (history || []).slice().sort((a, b) => a[0] - b[0])) {
    if (!finite(e)) continue;
    if (peak === null || e > peak) peak = e;
    else if (peak > 0 && (peak - e) / peak * 100 > best.pct) best = { usdt: peak - e, pct: (peak - e) / peak * 100 };
  }
  return peak === null ? null : best;
}
const ratio = v => finite(v) ? fmt(v, 2) : '—';
export function tradeStatsMarkup(doc, snapshot) {
  if (!doc && !snapshot) return '<p class="skeleton" role="status">数据加载中</p>';
  const s = doc?.stats || {}, o = s.overall || {}, dd = s.equity_drawdown || curveDrawdown(snapshot?.history);
  const n = finite(o.trades) ? o.trades : (doc?.trades || []).length;
  const cells = [
    ['已平仓', `${n} 笔`, '', finite(o.tp) || finite(o.sl) ? `止盈 ${o.tp ?? 0} · 止损 ${o.sl ?? 0}` : ''],
    ['胜率', finite(num(o.win_rate)) ? `${fmt(num(o.win_rate), 1)}%` : '—', '', finite(o.wins) ? `${o.wins} 胜 ${o.losses ?? 0} 负` : ''],
    ['盈亏比', ratio(num(o.payoff)), '', finite(num(o.avg_win)) || finite(num(o.avg_loss)) ? `均盈 ${finite(num(o.avg_win)) ? fmt(num(o.avg_win)) : '—'} / 均亏 ${finite(num(o.avg_loss)) ? fmt(num(o.avg_loss)) : '—'}` : '平均盈利 ÷ 平均亏损'],
    ['平均 R', finite(num(o.avg_r)) ? `${signed(num(o.avg_r))} R` : '—', tone(num(o.avg_r)), '每笔盈亏 ÷ 开仓风险'],
    ['已实现盈亏', finite(num(o.pnl)) ? `${signed(num(o.pnl))} U` : '—', tone(num(o.pnl)), finite(num(o.profit_factor)) ? `盈利因子 ${fmt(num(o.profit_factor))}` : ''],
    ['最大回撤', dd && finite(num(dd.pct)) ? `${fmt(num(dd.pct))}%` : '—', dd && num(dd.pct) > 0 ? 'down' : '', dd && finite(num(dd.usdt)) ? `权益曲线 −${fmt(num(dd.usdt))} U` : '按权益曲线'],
  ];
  const grid = `<div class="stat-grid">${cells.map(([k, v, c, sub]) => `<div><span>${k}</span><strong class="${c}">${v}</strong>${sub ? `<small>${esc(sub)}</small>` : ''}</div>`).join('')}</div>`;
  const seeds = s.by_strategy || [];
  const table = seeds.length ? `<div class="table-scroll"><table class="seed-table"><thead><tr><th>种子 / 策略</th><th>笔数</th><th>胜率</th><th>盈亏 USDT</th><th>盈亏比</th><th>平均 R</th></tr></thead><tbody>` +
    seeds.map(r => `<tr><td>${esc(r.key)}</td><td>${esc(r.trades)}</td><td>${finite(num(r.win_rate)) ? fmt(num(r.win_rate), 1) + '%' : '—'}</td>` +
      `<td class="${tone(num(r.pnl))}">${finite(num(r.pnl)) ? signed(num(r.pnl)) : '—'}</td><td>${ratio(num(r.payoff))}</td><td class="${tone(num(r.avg_r))}">${finite(num(r.avg_r)) ? signed(num(r.avg_r)) : '—'}</td></tr>`).join('') +
    '</tbody></table></div>' : '<p class="empty">按种子统计会在第一笔平仓后出现</p>';
  return grid + `<h3 class="seed-title">各种子表现</h3>` + table;
}

// ---- candle chart for a position / closed trade -------------------------------------------------
// What to draw: entry (and exit) markers and the stop / target lines. Works for a live position and a closed trade.
export function chartSubject(item, kind = 'position') {
  if (!item || !isInst(item.inst)) return null;
  const closed = kind === 'trade';
  const opened = Date.parse(item.opened_at), ended = closed ? Date.parse(item.closed_at) : NaN;
  const levels = [
    ['入场', num(closed ? item.entry : item.avgPx), 'entry'],
    ['止损', num(item.stop), 'stop'],
    ['止盈', num(item.target), 'target'],
    ...(closed ? [['出场', num(item.exit), 'exit']] : []),
  ].filter(([, v]) => finite(v) && v > 0).map(([title, price, role]) => ({ title, price, role }));
  return { inst: item.inst, side: sideNum(item.side), opened: finite(opened) ? opened : null, closed: finite(ended) ? ended : null, levels,
    title: `${coin(item.inst)} ${sideNum(item.side) === 1 ? '多' : '空'}${closed ? ' · 已平仓' : ' · 持仓中'}`,
    mark: closed ? null : num(item.mark ?? item.markPx) };
}
const BARS = [['15m', 15], ['1H', 60], ['4H', 240], ['1Dutc', 1440]];
// Pick a bar so the holding period spans at most ~80 bars; recent windows use market/candles (last 300 bars),
// older closed trades market/history-candles ending a little after the close.
export function candleRequest(subject, now = Date.now()) {
  const start = subject?.opened ?? now - 2 * 86400000, end = subject?.closed ?? now;
  const span = Math.max(0, end - start) / 60000;
  const [bar, mins] = BARS.find(([, m]) => span / m <= 80) || BARS.at(-1);
  const ms = mins * 60000;
  if (now - start + 20 * ms <= 300 * ms) return { path: 'market/candles', params: { instId: subject.inst, bar, limit: '300' }, bar };
  return { path: 'market/history-candles', params: { instId: subject.inst, bar, limit: '100', after: String(Math.min(now, end + 30 * ms)) }, bar };
}
const ROLE_COLOR = { entry: '#345db5', stop: '#c53045', target: '#087c50', exit: '#916000' };
export const levelColor = role => ROLE_COLOR[role] || '#536176';
// Fallback when candles cannot be loaded: the levels as a horizontal ladder (no chart library or network needed).
export function levelsLadderSVG(subject) {
  const rows = (subject?.levels || []).slice();
  if (finite(subject?.mark) && subject.mark > 0) rows.push({ title: '标记价', price: subject.mark, role: 'mark' });
  if (!rows.length) return '<p class="empty">这笔交易没有可显示的价位</p>';
  const vals = rows.map(r => r.price), lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * .12 || hi * .01;
  const W = 520, H = 220, y = v => 14 + (hi + pad - v) / (hi - lo + 2 * pad) * (H - 28);
  // Labels keep at least 16 px apart (close prices such as entry and mark would overlap); the lines stay exact.
  let last = -Infinity;
  return `<svg class="levels-ladder" viewBox="0 0 ${W} ${H + 16}" role="img" aria-label="入场 / 止损 / 止盈价位">` + rows.sort((a, b) => b.price - a.price).map(r => {
    const yy = y(r.price), ly = Math.max(yy, last + 16), c = levelColor(r.role); last = ly;
    return `<line x1="20" x2="${W - 150}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" stroke="${c}" stroke-width="2"${r.role === 'mark' ? ' stroke-dasharray="4 4"' : ''}/>` +
      `<text x="${W - 140}" y="${(ly + 4).toFixed(1)}" fill="${c}">${esc(r.title)} ${esc(px(r.price))}</text>`;
  }).join('') + '</svg>';
}
// Markers for lightweight-charts: entry (and exit) placed on the bar that contains the time.
export function tradeMarkers(subject, bars) {
  if (!bars?.length) return [];
  const at = ms => { const t = ms / 1000; let pick = null; for (const b of bars) { if (b.time <= t) pick = b.time; else break; } return pick; };
  const out = [];
  if (subject.opened) { const t = at(subject.opened); if (t !== null) out.push({ time: t, position: subject.side === 1 ? 'belowBar' : 'aboveBar', color: levelColor('entry'), shape: subject.side === 1 ? 'arrowUp' : 'arrowDown', text: '入场' }); }
  if (subject.closed) { const t = at(subject.closed); if (t !== null) out.push({ time: t, position: subject.side === 1 ? 'aboveBar' : 'belowBar', color: levelColor('exit'), shape: 'circle', text: '出场' }); }
  return out.sort((a, b) => a.time - b.time);
}

// ---- server read-only API (/live.json) ----
// {snapshot, fills, trades} when the document is valid (fills / trades may be missing or invalid: null), else null.
export function splitLiveApi(doc) {
  if (!doc || typeof doc !== 'object' || doc.source !== 'okx-live-api' || !validLiveSnapshot(doc.snapshot)) return null;
  return { snapshot: doc.snapshot, fills: validLiveFills(doc.fills) ? doc.fills : null, trades: validLiveTrades(doc.trades) ? doc.trades : null,
    account: doc.api?.account === 'okx' ? 'okx' : 'file' };
}
// Server tickers (/tickers.json) in the shape of OKX market/tickers rows, or null.
export function serverTickerRows(doc, now = Date.now()) {
  if (!doc || typeof doc !== 'object' || !Array.isArray(doc.tickers) || !stamp(doc.updated_at)) return null;
  if (now - Date.parse(doc.updated_at) > API_FRESH) return null;
  const rows = doc.tickers.filter(r => r && isInst(r.instId) && num(r.last) > 0);
  return rows.length ? rows : null;
}
// Where the account and the prices come from, for the status pill and the connection badge.
//   source: 'api' | 'github' | null    quotes: 'ws' | 'rest' | 'server' | null
export function sourceText(source, account) {
  if (source === 'api') return account === 'okx' ? '实盘账户来自服务器只读接口（约每分钟刷新，只读、白名单字段、无密钥）。' : '实盘账户来自服务器只读接口（服务器暂时读不到 OKX，显示最近一次交易程序快照）。';
  if (source === 'github') return '服务器接口暂时连不上，实盘账户改用 GitHub live-data 分支（约每小时同步，只读、无密钥）。';
  return '正在读取实盘账户…';
}
export function connectionBadge({ ws = false, rest = false, server = false } = {}) {
  if (ws) return { text: '行情实时', ok: true };
  if (rest) return { text: '行情轮询', ok: true };
  if (server) return { text: '服务器行情', ok: true };
  return { text: '行情断开', ok: false };
}

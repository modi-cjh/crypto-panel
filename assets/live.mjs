// OKX live account (real money), published by tools/live_sync.py on the live-data branch:
//   snapshot.json  equity / available / day PnL / paused / halted / positions with stop and target
//   fills.json     recent opens / closes and a short list of skips / pauses
// Pure functions only (rendering to HTML strings); the page wiring lives in app.mjs.
import { coinIcon } from './coins.mjs';

export const LIVE_BASE = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/live-data/';
export const LIVE_SNAPSHOT_URL = LIVE_BASE + 'snapshot.json';
export const LIVE_FILLS_URL = LIVE_BASE + 'fills.json';
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
      `<span class="okx-pos-tag">${tag}</span>` +
      `<span class="okx-pos-pnl ${tone(p.upl)}"><b>${finite(p.upl) ? `${signed(p.upl)} USDT` : '—'}</b><small>${pct === null ? '' : `${signed(pct)}%`}</small></span></summary>` +
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

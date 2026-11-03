// OKX demo-trading account (tools/okx_demo_trader.py) published on the paper-data branch.
// The strategy engine (tools/paper_trader.py) still runs in the background and supplies the signals;
// the dashboard shows only the exchange account.
import { coinIcon } from './coins.mjs';

export const OKX_URL = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/okx_demo/summary.json';
export const OKX_STALE = 3 * 3600000;      // runs every 30 minutes; GitHub cron can lag
export const OKX_COLOR = '#06727d';
const RECENT = 3 * 3600000;
const DEFAULT_START = 5000;
const finite = Number.isFinite;
const num = v => v === null || v === undefined || v === '' ? NaN : Number(v);
const symbol = s => typeof s === 'string' && /^[A-Z0-9]+-USDT-SWAP$/.test(s);
const stamp = t => typeof t === 'string' && finite(Date.parse(t));
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = (v, d = 2) => finite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—';
const signed = (v, d = 2) => finite(v) ? (v > 0 ? '+' : '') + fmt(v, d) : '—';
const tone = v => finite(v) ? v >= 0 ? 'up' : 'down' : '';
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const time = t => finite(typeof t === 'number' ? t : Date.parse(t)) ? clock.format(typeof t === 'number' ? t : Date.parse(t)) : '—';
const px = v => fmt(v, Math.abs(v) >= 100 ? 1 : Math.abs(v) >= 1 ? 3 : 5);
const coin = inst => String(inst).split('-')[0];
const STRATEGIES = { mean_reversion: '均值回归', breakout_long: '趋势突破只做多', breakout_daily: '日线趋势突破' };
const ACTIONS = { open: '开仓', close: '平仓', amend_stop: '上移止损', closed_on_exchange: '交易所止损 / 止盈触发', skip: '跳过', daily_halt: '日亏停手',
  pause: '暂停跟单', resume: '恢复跟单', flatten: '全部平仓并暂停', error: '出错' };
const SKIPS = { paper_entry_too_old: '信号已超过 2 小时', stop_too_close_or_crossed: '价格已接近或越过止损', target_already_reached: '价格已到止盈',
  size_below_minimum: '仓位小于最小下单量', insufficient_margin: '保证金不足', exchange_position_exists_not_ours: '交易所已有非本程序的仓位',
  no_price: '取不到价格', no_instrument_spec: '取不到合约信息' };
const CLOSES = { paper_closed: '策略信号平仓', daily_halt: '日亏停手平仓', manual_flatten: '手动全部平仓' };

export function validOkx(d) {
  return d !== null && typeof d === 'object' && stamp(d.updated_at) && finite(d.equity) && Array.isArray(d.positions) &&
    d.positions.every(p => p && symbol(p.inst)) && Array.isArray(d.recent_actions || []) &&
    (d.history === undefined || Array.isArray(d.history) && d.history.every(p => Array.isArray(p) && finite(p[0]) && finite(p[1])));
}

const sideOf = p => p.side === 'long' ? 1 : p.side === 'short' ? -1 : num(p.contracts) >= 0 ? 1 : -1;
export const okxStart = d => finite(num(d?.start_equity)) && d.start_equity > 0 ? Number(d.start_equity) : d?.history?.[0]?.[1] > 0 ? d.history[0][1] : DEFAULT_START;

// Floating PnL follows live OKX prices when the contract value is known; otherwise the values recorded on the last run.
export function okxLive(d, tickers = [], priceAt = 0, now = Date.now()) {
  const fresh = priceAt > 0 && now - priceAt <= 24000;
  const positions = d.positions.map(p => {
    const last = tickers.find(t => t.id === p.inst)?.last, ct = num(p.ctVal), qty = Math.abs(num(p.contracts)), entry = num(p.avgPx);
    if (fresh && last > 0 && ct > 0 && qty > 0 && entry > 0) return { ...p, side: sideOf(p), mark: last, upl: sideOf(p) * qty * ct * (last - entry), live: true };
    return { ...p, side: sideOf(p), mark: num(p.markPx), upl: num(p.upl), live: false };
  });
  const recorded = d.positions.reduce((s, p) => s + (finite(num(p.upl)) ? num(p.upl) : 0), 0);
  const live = positions.length > 0 && positions.every(p => p.live);
  return { equity: live ? d.equity - recorded + positions.reduce((s, p) => s + p.upl, 0) : d.equity, positions, live };
}

export function okxTotals(d, tickers = [], priceAt = 0, now = Date.now()) {
  if (!d) return null;
  const l = okxLive(d, tickers, priceAt, now), start = okxStart(d);
  const dayStart = num(d.day_start), day = dayStart > 0 ? l.equity - dayStart : null;
  return { equity: l.equity, start, ret: (l.equity / start - 1) * 100, day, dayPct: day == null ? null : day / dayStart * 100,
    positions: d.positions.length, trades: finite(num(d.closed_trades)) ? Number(d.closed_trades) : 0, live: l.live };
}

export function okxStatus(d, now = Date.now()) {
  if (now - Date.parse(d.updated_at) > OKX_STALE) return { label: '数据延迟', cls: 'down' };
  if (d.halted) return { label: '日亏停手', cls: 'down' };
  if (d.paused) return { label: '已暂停', cls: 'muted' };
  return { label: '运行中', cls: 'up' };
}

export function okxCurveSVG(d) {
  const pts = (d.history || []).slice().sort((a, b) => a[0] - b[0]);
  if (pts.length < 2 || pts.at(-1)[0] === pts[0][0]) return '<p class="empty">资金曲线会在跟单运行几次后出现（约每 30 分钟一个点）</p>';
  const base = okxStart(d), t0 = pts[0][0], t1 = pts.at(-1)[0], vals = pts.map(p => p[1]);
  const lo = Math.min(...vals, base), hi = Math.max(...vals, base), pad = Math.max((hi - lo) * .12, base * .002);
  const W = 680, H = 230, L = 62, R = 12, T = 14, B = 34, x = t => L + (t - t0) / (t1 - t0) * (W - L - R), y = v => T + (hi + pad - v) / (hi - lo + pad * 2) * (H - T - B);
  const grid = [hi + pad, base, lo - pad].map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === base ? 'sim-base' : 'equity-grid'}"/><text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${fmt(v)}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="OKX 模拟账户资金曲线">${grid}<polyline points="${pts.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')}" fill="none" stroke="${OKX_COLOR}" stroke-width="2" vector-effect="non-scaling-stroke"/>` +
    `<text x="${L}" y="${H - 10}">${time(t0)}</text><text x="${W - R}" y="${H - 10}" text-anchor="end">${time(t1)} UTC+7</text></svg>`;
}

export function okxPositionsMarkup(d, tickers = [], priceAt = 0, now = Date.now()) {
  const { positions } = okxLive(d, tickers, priceAt, now);
  if (!positions.length) return '<p class="empty">当前没有持仓，等待策略信号</p>';
  const open = Object.fromEntries((d.open_mirrors || []).map(m => [m.inst, m]));
  return positions.map(p => {
    const m = open[p.inst], margin = num(p.margin), pct = margin > 0 && finite(p.upl) ? p.upl / margin * 100 : null;
    return `<article class="history-card sim-position" style="--sim-color:${OKX_COLOR}"><div class="history-card-head"><h3 class="coin-label">${coinIcon(p.inst)}${esc(coin(p.inst))} <span class="${p.side === 1 ? 'up' : 'down'}">${p.side === 1 ? '多' : '空'} · ${esc(p.lever)}×</span></h3><span class="pill">${p.ours ? esc(STRATEGIES[m?.strategy] || '跟单') : '非本程序仓位'}</span></div><div class="history-metrics">` +
      [['张数', esc(Math.abs(num(p.contracts)))], ['开仓均价', px(num(p.avgPx))], ['止损', m && finite(num(m.stop)) ? px(num(m.stop)) : '—'], ['保证金 USDT', fmt(margin)],
       [p.live ? '现价（实时）' : `标记价（${time(d.updated_at)}）`, px(p.mark)], ['浮动盈亏', finite(p.upl) ? `${signed(p.upl)} USDT${pct == null ? '' : ` / ${signed(pct)}%`}` : '—', tone(p.upl)]]
        .map(([k, v, c = '']) => `<div><span>${k}</span><strong class="${c}">${v}</strong></div>`).join('') + '</div></article>';
  }).join('');
}

function actionDetail(a) {
  if (a.action === 'skip') return SKIPS[a.reason] || a.reason || '';
  if (a.action === 'open') return `${STRATEGIES[a.strategy] ? STRATEGIES[a.strategy] + ' · ' : ''}${a.side === 1 ? '多' : '空'} ${a.contracts} 张 @ ${a.price} · 止损 ${a.stop}${a.target ? ' · 止盈 ' + a.target : ''} · ${a.leverage}×`;
  if (a.action === 'amend_stop') return `止损 ${a.old} → ${a.new}`;
  if (a.action === 'close') return CLOSES[a.reason] || a.reason || '';
  if (a.action === 'daily_halt') return '当日亏损达到 8%，全部平仓，次日恢复';
  if (a.action === 'error') return a.detail || a.step || '';
  return '';
}

export function okxActionsMarkup(d, limit = 10) {
  const rows = (d.recent_actions || []).slice(0, limit);
  if (!rows.length) return '<li class="empty">还没有交易动作</li>';
  return rows.map(a => `<li><div><span>${esc(ACTIONS[a.action] || a.action)}${a.inst && a.inst !== '-' ? ' · ' + esc(coin(a.inst)) : ''}${a.dry_run ? ' <small class="muted">(空跑)</small>' : ''}</span><time>${time(a.ts)}</time></div><div><small class="muted">${esc(actionDetail(a))}</small></div></li>`).join('');
}

const feedLine = a => a.action === 'open' ? { text: `开仓 ${coin(a.inst)} ${a.side === 1 ? '多' : '空'} ${a.contracts} 张 @ ${a.price}`, mood: 'excited' }
  : a.action === 'close' ? { text: `平仓 ${coin(a.inst)} · ${CLOSES[a.reason] || '策略信号'}`, mood: 'happy' }
  : a.action === 'closed_on_exchange' ? { text: `${coin(a.inst)} 交易所止损 / 止盈触发`, mood: 'sad' }
  : a.action === 'daily_halt' ? { text: '触发日亏停手，全部平仓，今天不再开新仓', mood: 'sad' }
  : a.action === 'error' ? { text: `跟单出错${a.inst && a.inst !== '-' ? ' · ' + coin(a.inst) : ''}`, mood: 'sad' } : null;
const actionKey = a => `${a.ts}|${a.action}|${a.inst}`;

export function okxFeed(d, limit = 20) {
  return (d?.recent_actions || []).map(a => ({ ts: Date.parse(a.ts), ...feedLine(a) })).filter(r => r.text && finite(r.ts)).slice(0, limit);
}

// Actions that appeared since the previous summary, oldest first, for toasts and the companion.
export function okxChanges(prev, next) {
  if (!prev || !next) return [];
  const seen = new Set((prev.recent_actions || []).map(actionKey));
  return (next.recent_actions || []).filter(a => !seen.has(actionKey(a))).reverse().map(feedLine).filter(Boolean);
}

export function okxMascotState(d, now = Date.now()) {
  if (!d || now - Date.parse(d.updated_at) > OKX_STALE) return { state: 'offline', label: '离线', message: 'OKX 模拟账户超过 3 小时没有更新，我在等下一次运行' };
  if (d.halted) return { state: 'paused', label: '停手', message: '今日触发日亏停手，不再开新仓' };
  if (d.paused) return { state: 'paused', label: '暂停', message: '跟单已暂停，恢复后我会播报开平仓' };
  const recent = (d.recent_actions || []).filter(a => finite(Date.parse(a.ts)) && now - Date.parse(a.ts) >= 0 && now - Date.parse(a.ts) <= RECENT);
  const stop = recent.find(a => a.action === 'closed_on_exchange');
  if (stop) return { state: 'sad', label: '刚止损', message: `${coin(stop.inst)} 交易所止损 / 止盈触发` };
  const open = recent.find(a => a.action === 'open');
  if (open) return { state: 'excited', label: '刚开仓', message: `刚开仓 ${coin(open.inst)} ${open.side === 1 ? '多' : '空'}` };
  const t = okxTotals(d);
  if (t.ret > 0) return { state: 'happy', label: '盈利中', message: `OKX 模拟账户累计 ${signed(t.ret)}%` };
  return { state: 'idle', label: '待命', message: t.positions ? `持仓 ${t.positions} 个，止损挂在交易所` : '空仓中，等待新信号' };
}

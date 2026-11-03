// Learning-only paper accounts produced hourly by tools/paper_trader.py on the paper-data branch.
import { coinIcon } from './coins.mjs';
export const SIM_URL = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/dashboard.json';
export const SIM_COLORS = { mean_reversion: '#c53045', breakout_long: '#916000', breakout_daily: '#087c50' };
const finite = Number.isFinite;
const symbol = s => typeof s === 'string' && /^[A-Z0-9]+-USDT-SWAP$/.test(s);
const stamp = t => typeof t === 'string' && finite(Date.parse(t));
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = (v, d = 2) => finite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—';
const signed = (v, d = 2) => finite(v) ? (v > 0 ? '+' : '') + fmt(v, d) : '—';
const tone = v => finite(v) ? v >= 0 ? 'up' : 'down' : '';
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const time = t => finite(Date.parse(t)) ? clock.format(Date.parse(t)) : '—';
const REASONS = { stop: '止损 / 跟随止损', target: '止盈', timeout: '到期平仓' };

export function validSim(data) {
  const obj = v => v !== null && typeof v === 'object' && !Array.isArray(v);
  if (!obj(data) || data.schema_version !== 1 || !stamp(data.updated_at) || !obj(data.accounts) || !Object.keys(data.accounts).length) return false;
  return Object.values(data.accounts).every(a => obj(a) && typeof a.name === 'string' && finite(a.equity) && finite(a.return_pct) && finite(a.max_drawdown_pct) &&
    Number.isInteger(a.closed_trades) && Array.isArray(a.curve) && a.curve.every(p => Array.isArray(p) && Number.isInteger(p[0]) && finite(p[1])) &&
    Array.isArray(a.open_positions) && a.open_positions.every(p => symbol(p.inst) && [1, -1].includes(p.side) && ['entry', 'stop', 'qty', 'margin'].every(k => finite(p[k]) && p[k] > 0) && stamp(p.opened)) &&
    Array.isArray(a.trades) && a.trades.every(t => symbol(t.inst) && [1, -1].includes(t.side) && finite(t.pnl) && finite(t.r) && stamp(t.exit_time)));
}

// Floating PnL at the live price when it is fresh; otherwise null (the hourly equity already includes the last close).
export function livePnl(position, tickers = [], priceAt = 0, now = Date.now()) {
  const last = tickers.find(t => t.id === position.inst)?.last;
  if (!(last > 0) || !(priceAt > 0) || now - priceAt > 24000) return { mark: null, pnl: null, pct: null };
  const pnl = Math.max(-position.margin, position.side * position.qty * (last - position.entry));
  return { mark: last, pnl, pct: pnl / position.margin * 100 };
}

export function simTotals(data) {
  const accounts = Object.values(data?.accounts || {});
  const equity = accounts.reduce((s, a) => s + a.equity, 0), start = (data?.start_equity || 100) * accounts.length;
  return { equity, start, ret: start ? (equity / start - 1) * 100 : null, positions: accounts.reduce((s, a) => s + a.open_positions.length, 0), trades: accounts.reduce((s, a) => s + a.closed_trades, 0), count: accounts.length };
}

export function simSummaryMarkup(data) {
  if (!data) return '';
  const t = simTotals(data);
  return `<span class="eyebrow">PAPER / 学习用模拟交易</span><strong>${t.count} 个账户 · 合计 ${fmt(t.equity)} USDT <span class="${tone(t.ret)}">${signed(t.ret)}%</span></strong><small>持仓 ${t.positions} · 已平仓 ${t.trades} 笔 · 更新 ${time(data.updated_at)} UTC+7 · 查看详情 →</small>`;
}

export function simCardsMarkup(data) {
  return Object.entries(data.accounts).map(([key, a]) => `<article class="history-card sim-card" style="--sim-color:${SIM_COLORS[key] || '#06727d'}"><div class="history-card-head"><h3><i class="sim-dot" aria-hidden="true"></i>${esc(a.name)}</h3>${a.halted ? '<span class="pill down">今日停手</span>' : ''}</div><p class="panel-caption">${esc(a.rules)}</p><div class="history-metrics">` +
    [['权益 USDT', fmt(a.equity)], ['收益', `${signed(a.return_pct)}%`, tone(a.return_pct)], ['最大回撤', `${fmt(a.max_drawdown_pct)}%`, a.max_drawdown_pct > 0 ? 'down' : ''], ['已平仓', `${a.closed_trades} 笔`],
     ['胜率', a.win_rate_pct == null ? '—' : `${fmt(a.win_rate_pct, 1)}%`], ['平均 R', a.avg_r == null ? '—' : signed(a.avg_r)]]
      .map(([k, v, c = '']) => `<div><span>${k}</span><strong class="${c}">${v}</strong></div>`).join('') + `</div><small class="muted">持仓 ${a.open_positions.length} · 最新 K 线 ${time(a.last_bar)} UTC+7</small></article>`).join('');
}

export function simCurveSVG(data) {
  const series = Object.entries(data.accounts).map(([key, a]) => ({ key, name: a.name, pts: a.curve })).filter(s => s.pts.length);
  const all = series.flatMap(s => s.pts);
  if (all.length < 2) return '<p class="empty">资金曲线将在第一根新 K 线后出现（每小时更新）</p>';
  const t0 = Math.min(...all.map(p => p[0])), t1 = Math.max(...all.map(p => p[0])) || t0 + 1;
  const vals = all.map(p => p[1]), lo = Math.min(...vals, 100), hi = Math.max(...vals, 100), pad = Math.max((hi - lo) * .12, .5);
  const W = 680, H = 230, L = 52, R = 12, T = 14, B = 34, x = t => L + (t - t0) / ((t1 - t0) || 1) * (W - L - R), y = v => T + (hi + pad - v) / (hi - lo + pad * 2) * (H - T - B);
  const grid = [hi + pad, 100, lo - pad].map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 100 ? 'sim-base' : 'equity-grid'}"/><text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${fmt(v)}</text>`).join('');
  const lines = series.map(s => `<polyline points="${s.pts.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')}" fill="none" stroke="${SIM_COLORS[s.key] || '#06727d'}" stroke-width="2" vector-effect="non-scaling-stroke"/>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="三个模拟账户资金曲线">${grid}${lines}<text x="${L}" y="${H - 10}">${time(new Date(t0).toISOString())}</text><text x="${W - R}" y="${H - 10}" text-anchor="end">${time(new Date(t1).toISOString())} UTC+7</text></svg>` +
    `<div class="sim-legend">${series.map(s => `<span><i style="background:${SIM_COLORS[s.key]}"></i>${esc(s.name)}</span>`).join('')}</div>`;
}

export function simPositionsMarkup(data, tickers, priceAt, now = Date.now()) {
  const rows = Object.entries(data.accounts).flatMap(([key, a]) => a.open_positions.map(p => ({ key, account: a.name, ...p, ...livePnl(p, tickers, priceAt, now) })));
  if (!rows.length) return '<p class="empty">当前没有持仓</p>';
  return rows.map(p => `<article class="history-card sim-position" style="--sim-color:${SIM_COLORS[p.key] || '#06727d'}"><div class="history-card-head"><h3 class="coin-label">${coinIcon(p.inst)}${esc(p.inst.split('-')[0])} <span class="${p.side === 1 ? 'up' : 'down'}">${p.side === 1 ? '多' : '空'} · ${p.leverage}×</span></h3><span class="pill">${esc(p.account)}</span></div><div class="history-metrics">` +
    [['开仓价', fmt(p.entry, 6)], ['止损', fmt(p.stop, 6)], ['止盈', p.target == null ? '跟随止损' : fmt(p.target, 6)], ['保证金 USDT', fmt(p.margin)],
     ['现价', p.mark == null ? '等待行情' : fmt(p.mark, 6)], ['浮动盈亏', p.pnl == null ? '—' : `${signed(p.pnl)} USDT / ${signed(p.pct)}%`, tone(p.pnl)]]
      .map(([k, v, c = '']) => `<div><span>${k}</span><strong class="${c}">${v}</strong></div>`).join('') + `</div><small class="muted">开仓 ${time(p.opened)} UTC+7</small></article>`).join('');
}

export function simTradesMarkup(data, limit = 20) {
  const rows = Object.entries(data.accounts).flatMap(([key, a]) => a.trades.map(t => ({ key, account: a.name, ...t }))).sort((a, b) => Date.parse(b.exit_time) - Date.parse(a.exit_time)).slice(0, limit);
  if (!rows.length) return '<li class="empty">还没有平仓记录</li>';
  return rows.map(t => `<li><div><span class="coin-label">${coinIcon(t.inst)}${esc(t.inst.split('-')[0])}</span><span class="${t.side === 1 ? 'up' : 'down'}">${t.side === 1 ? '多' : '空'}</span><span class="pill" style="--sim-color:${SIM_COLORS[t.key] || '#06727d'}">${esc(t.account)}</span><time>${time(t.exit_time)}</time></div><div><span>${esc(REASONS[t.reason] || t.reason)} · ${signed(t.r)}R</span><span class="${tone(t.pnl)}">${signed(t.pnl)} USDT</span></div></li>`).join('');
}

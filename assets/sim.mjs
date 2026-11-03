// Learning-only paper accounts produced hourly by tools/paper_trader.py on the paper-data branch.
import { coinIcon } from './coins.mjs';
export const SIM_URL = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/dashboard.json';
export const SIM_COLORS = { combined: '#06727d', mean_reversion: '#c53045', breakout_long: '#916000', breakout_daily: '#087c50' };
// In the combined account every position and trade names the strategy that made it.
const who = (a, item) => item.strategy_name || a.name;
const colorOf = (key, item) => SIM_COLORS[item.strategy] || SIM_COLORS[key] || '#06727d';
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

// Floating PnL at the live OKX price when the browser has a fresh one; otherwise the price the paper
// trader recorded on its last run (browsers that cannot reach OKX still see PnL, a little older).
export function livePnl(position, tickers = [], priceAt = 0, now = Date.now()) {
  const last = tickers.find(t => t.id === position.inst)?.last;
  if (last > 0 && priceAt > 0 && now - priceAt <= 24000) {
    const pnl = Math.max(-position.margin, position.side * position.qty * (last - position.entry));
    return { mark: last, pnl, pct: pnl / position.margin * 100, live: true };
  }
  if (finite(position.mark) && position.mark > 0 && finite(position.unrealized))
    return { mark: position.mark, pnl: position.unrealized, pct: position.unrealized / position.margin * 100, live: false };
  return { mark: null, pnl: null, pct: null, live: false };
}

// Equity moving with prices: realised cash plus each open position's floating PnL. Falls back to the
// equity recorded on the last run when the cash balance or any position's price is unavailable.
export function liveEquity(account, tickers = [], priceAt = 0, now = Date.now()) {
  if (!finite(account.cash)) return { equity: account.equity, live: false };
  const marks = account.open_positions.map(p => livePnl(p, tickers, priceAt, now));
  if (marks.some(m => m.pnl == null)) return { equity: account.equity, live: false };
  return { equity: account.cash + marks.reduce((s, m) => s + m.pnl, 0), live: marks.length > 0 && marks.every(m => m.live) };
}

export function simTotals(data, tickers = [], priceAt = 0, now = Date.now()) {
  const accounts = Object.values(data?.accounts || {});
  const equity = accounts.reduce((s, a) => s + liveEquity(a, tickers, priceAt, now).equity, 0), start = (data?.start_equity || 100) * accounts.length;
  return { equity, start, ret: start ? (equity / start - 1) * 100 : null, positions: accounts.reduce((s, a) => s + a.open_positions.length, 0), trades: accounts.reduce((s, a) => s + a.closed_trades, 0), count: accounts.length };
}

export function simSummaryMarkup(data, tickers, priceAt, now = Date.now()) {
  if (!data) return '';
  const t = simTotals(data, tickers, priceAt, now);
  return `<span class="eyebrow">PAPER / 学习用模拟交易</span><strong>${t.count === 1 ? esc(Object.values(data.accounts)[0].name) + ' · 权益' : t.count + ' 个账户 · 合计'} ${fmt(t.equity)} USDT <span class="${tone(t.ret)}">${signed(t.ret)}%</span></strong><small>持仓 ${t.positions} · 已平仓 ${t.trades} 笔 · 更新 ${time(data.updated_at)} UTC+7 · 查看详情 →</small>`;
}

export function simCardsMarkup(data, tickers, priceAt, now = Date.now()) {
  return Object.entries(data.accounts).map(([key, a]) => ({ key, a, eq: liveEquity(a, tickers, priceAt, now).equity })).map(({ key, a, eq }) => `<article class="history-card sim-card" style="--sim-color:${SIM_COLORS[key] || '#06727d'}"><div class="history-card-head"><h3><i class="sim-dot" aria-hidden="true"></i>${esc(a.name)}</h3>${a.halted ? '<span class="pill down">今日停手</span>' : ''}</div><p class="panel-caption">${esc(a.rules)}</p><div class="history-metrics">` +
    [['权益 USDT', fmt(eq)], ['收益', `${signed((eq / (data.start_equity || 100) - 1) * 100)}%`, tone(eq - (data.start_equity || 100))], ['最大回撤', `${fmt(a.max_drawdown_pct)}%`, a.max_drawdown_pct > 0 ? 'down' : ''], ['已平仓', `${a.closed_trades} 笔`],
     ['胜率', a.win_rate_pct == null ? '—' : `${fmt(a.win_rate_pct, 1)}%`], ['平均 R', a.avg_r == null ? '—' : signed(a.avg_r)]]
      .map(([k, v, c = '']) => `<div><span>${k}</span><strong class="${c}">${v}</strong></div>`).join('') + `</div>${strategyBreakdown(a)}<small class="muted">持仓 ${a.open_positions.length} · 最新 K 线 ${time(a.last_bar)} UTC+7</small></article>`).join('');
}

function strategyBreakdown(a) {
  if (!Array.isArray(a.by_strategy) || !a.by_strategy.length) return '';
  return `<table class="sim-strategies"><thead><tr><th>策略</th><th>已平仓</th><th>胜率</th><th>盈亏 USDT</th><th>持仓</th></tr></thead><tbody>${a.by_strategy.map(b => `<tr><td><i class="sim-dot" style="--sim-color:${SIM_COLORS[b.key] || '#06727d'}"></i>${esc(b.name)}</td><td>${b.closed_trades}</td><td>${b.win_rate_pct == null ? '—' : fmt(b.win_rate_pct, 1) + '%'}</td><td class="${tone(b.pnl)}">${signed(b.pnl)}</td><td>${b.positions}</td></tr>`).join('')}</tbody></table>`;
}

export function simCurveSVG(data) {
  const raw = Object.entries(data.accounts).map(([key, a]) => ({ key, name: a.name, pts: a.curve })).filter(s => s.pts.length);
  if (raw.flatMap(s => s.pts).length < 2) return '<p class="empty">资金曲线将在第一根新 K 线后出现（每小时更新）</p>';
  // Start where every account has data (a daily account's first point is a day older than the hourly ones);
  // an account carries its last earlier value forward to that start.
  const start = Math.max(...raw.map(s => s.pts[0][0]));
  const series = raw.map(s => { const before = s.pts.filter(p => p[0] <= start).at(-1); return { ...s, pts: [[start, before[1]], ...s.pts.filter(p => p[0] > start)] }; });
  const all = series.flatMap(s => s.pts);
  const t0 = start, t1 = Math.max(...all.map(p => p[0]));
  if (t1 === t0) return '<p class="empty">资金曲线将在第一根新 K 线后出现（每小时更新）</p>';
  const vals = all.map(p => p[1]), lo = Math.min(...vals, 100), hi = Math.max(...vals, 100), pad = Math.max((hi - lo) * .12, .5);
  const W = 680, H = 230, L = 52, R = 12, T = 14, B = 34, x = t => L + (t - t0) / ((t1 - t0) || 1) * (W - L - R), y = v => T + (hi + pad - v) / (hi - lo + pad * 2) * (H - T - B);
  const grid = [hi + pad, 100, lo - pad].map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === 100 ? 'sim-base' : 'equity-grid'}"/><text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${fmt(v)}</text>`).join('');
  const lines = series.map(s => `<polyline points="${s.pts.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')}" fill="none" stroke="${SIM_COLORS[s.key] || '#06727d'}" stroke-width="2" vector-effect="non-scaling-stroke"/>`).join('');
  const b = simLiveSince(data), live = b && b.live > t0 && b.live <= t1 ? `<line x1="${x(b.live).toFixed(1)}" x2="${x(b.live).toFixed(1)}" y1="${T}" y2="${H - B}" class="sim-live"/><text x="${(x(b.live) - 4).toFixed(1)}" y="${T + 10}" text-anchor="end" class="sim-live-label">← 历史补跑</text><text x="${(x(b.live) + 4).toFixed(1)}" y="${T + 10}" class="sim-live-label">实时 →</text>` : '';
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="模拟盘资金曲线">${grid}${live}${lines}<text x="${L}" y="${H - 10}">${time(new Date(t0).toISOString())}</text><text x="${W - R}" y="${H - 10}" text-anchor="end">${time(new Date(t1).toISOString())} UTC+7</text></svg>` +
    `<div class="sim-legend">${series.map(s => `<span><i style="background:${SIM_COLORS[s.key]}"></i>${esc(s.name)}</span>`).join('')}</div>`;
}

export function simPositionsMarkup(data, tickers, priceAt, now = Date.now()) {
  const rows = Object.entries(data.accounts).flatMap(([key, a]) => a.open_positions.map(p => ({ key, account: who(a, p), color: colorOf(key, p), ...p, ...livePnl(p, tickers, priceAt, now) })));
  const priceLabel = p => p.live ? '现价（实时）' : p.mark == null ? '现价' : `最新价（${time(data.updated_at)}）`;
  if (!rows.length) return '<p class="empty">当前没有持仓</p>';
  return rows.map(p => `<article class="history-card sim-position" style="--sim-color:${p.color}"><div class="history-card-head"><h3 class="coin-label">${coinIcon(p.inst)}${esc(p.inst.split('-')[0])} <span class="${p.side === 1 ? 'up' : 'down'}">${p.side === 1 ? '多' : '空'} · ${p.leverage}×</span></h3><span class="pill">${esc(p.account)}</span></div><div class="history-metrics">` +
    [['开仓价', fmt(p.entry, 6)], ['止损', fmt(p.stop, 6)], ['止盈', p.target == null ? '跟随止损' : fmt(p.target, 6)], ['保证金 USDT', fmt(p.margin)],
     [priceLabel(p), p.mark == null ? '等待行情' : fmt(p.mark, 6)], ['浮动盈亏', p.pnl == null ? '—' : `${signed(p.pnl)} USDT / ${signed(p.pct)}%`, tone(p.pnl)]]
      .map(([k, v, c = '']) => `<div><span>${k}</span><strong class="${c}">${v}</strong></div>`).join('') + `</div><small class="muted">开仓 ${time(p.opened)} UTC+7</small></article>`).join('');
}

export function simTradesMarkup(data, limit = 20) {
  const rows = Object.entries(data.accounts).flatMap(([key, a]) => a.trades.map(t => ({ key, account: who(a, t), color: colorOf(key, t), ...t }))).sort((a, b) => Date.parse(b.exit_time) - Date.parse(a.exit_time)).slice(0, limit);
  if (!rows.length) return '<li class="empty">还没有平仓记录</li>';
  return rows.map(t => `<li><div><span class="coin-label">${coinIcon(t.inst)}${esc(t.inst.split('-')[0])}</span><span class="${t.side === 1 ? 'up' : 'down'}">${t.side === 1 ? '多' : '空'}</span><span class="pill" style="--sim-color:${t.color}">${esc(t.account)}</span><time>${time(t.exit_time)}</time></div><div><span>${esc(REASONS[t.reason] || t.reason)} · ${signed(t.r)}R</span><span class="${tone(t.pnl)}">${signed(t.pnl)} USDT</span></div></li>`).join('');
}

// ---- Companion (mascot) driven by the paper accounts ----------------------------
const RECENT = 3 * 3600000;      // hourly data lands up to ~1h after the bar, plus GitHub cron delays
export const SIM_STALE = 3 * 3600000;
const sideText = s => s === 1 ? '多' : s === -1 ? '空' : '';
const coin = inst => String(inst).split('-')[0];

export function simMascotState(data, now = Date.now()) {
  if (!data || !finite(Date.parse(data.updated_at)) || now - Date.parse(data.updated_at) > SIM_STALE)
    return { state: 'offline', label: '离线', message: '模拟盘数据超过 3 小时未更新，我在等下一次运行' };
  const accounts = Object.values(data.accounts);
  const halted = accounts.filter(a => a.halted);
  if (halted.length) return { state: 'paused', label: '停手', message: `${halted.map(a => a.name).join('、')} 今日触发日亏停手，不再开新仓` };
  const recent = t => finite(Date.parse(t)) && now - Date.parse(t) >= 0 && now - Date.parse(t) <= RECENT;
  const stopped = accounts.flatMap(a => a.trades.filter(t => recent(t.exit_time) && t.pnl < 0).map(t => ({ a, t })));
  if (stopped.length) { const { a, t } = stopped[0]; return { state: 'sad', label: '刚止损', message: `${who(a, t)} ${coin(t.inst)} 止损 ${signed(t.pnl)} USDT，按计划休息` }; }
  const opened = accounts.flatMap(a => a.open_positions.filter(p => recent(p.opened)).map(p => ({ a, p })));
  if (opened.length) { const { a, p } = opened[0]; return { state: 'excited', label: '刚开仓', message: `${who(a, p)} 刚开仓 ${coin(p.inst)} ${sideText(p.side)}` }; }
  const t = simTotals(data);
  if (t.ret > 0) return { state: 'happy', label: '盈利中', message: `模拟盘累计 ${signed(t.ret)}%` };
  return { state: 'idle', label: '待命', message: t.positions ? `持仓 ${t.positions} 个，盯着止损和跟随止损` : '空仓中，等待新信号' };
}

// Lines to narrate when a newer dashboard arrives: opens, closes (with PnL) and new halts.
export function simChanges(prev, next) {
  if (!prev || !next) return [];
  const lines = [];
  for (const [key, a] of Object.entries(next.accounts)) {
    const before = prev.accounts?.[key];
    if (!before) continue;
    const had = new Set(before.open_positions.map(p => p.inst + '|' + p.opened));
    for (const p of a.open_positions) if (!had.has(p.inst + '|' + p.opened))
      lines.push({ text: `${who(a, p)} 开仓 ${coin(p.inst)} ${sideText(p.side)} @ ${+p.entry.toPrecision(6)}`, mood: 'excited' });
    const done = new Set(before.trades.map(t => t.inst + '|' + t.exit_time));
    for (const t of [...a.trades].reverse()) if (!done.has(t.inst + '|' + t.exit_time))
      lines.push({ text: `${who(a, t)} 平仓 ${coin(t.inst)} ${sideText(t.side)} ${signed(t.pnl)} USDT`, mood: t.pnl < 0 ? 'sad' : 'happy' });
    if (a.halted && !before.halted) lines.push({ text: `${a.name} 触发日亏停手，今天不再开新仓`, mood: 'sad' });
  }
  return lines;
}

export function simFeed(data, limit = 20) {
  return Object.values(data?.accounts || {}).flatMap(a => [
    ...a.trades.map(t => ({ ts: Date.parse(t.exit_time), text: `${who(a, t)} 平仓 ${coin(t.inst)} ${sideText(t.side)} ${signed(t.pnl)} USDT`, mood: t.pnl < 0 ? 'sad' : 'happy' })),
    ...a.open_positions.map(p => ({ ts: Date.parse(p.opened), text: `${who(a, p)} 开仓 ${coin(p.inst)} ${sideText(p.side)} @ ${+p.entry.toPrecision(6)}`, mood: 'excited' })),
  ]).filter(r => finite(r.ts)).sort((x, y) => y.ts - x.ts).slice(0, limit);
}

// Orders signalled on the last closed bar that fill at the next bar's open.
export function simPendingText(data) {
  const rows = Object.values(data?.accounts || {}).flatMap(a => Array.isArray(a.pending_detail) ? a.pending_detail.map(o => `${o.strategy_name} ${coin(o.inst)} ${sideText(o.side)}`) : (a.pending || []).map(inst => `${a.name} ${coin(inst)}`));
  return rows.length ? `待成交（下一根 K 线开盘模拟成交）：${rows.join('、')}` : '';
}

// Accounts can start in the past (--backfill-days): everything before live_since is a replay.
export function simLiveSince(data) {
  const rows = Object.values(data?.accounts || {}).filter(a => stamp(a.live_since) && stamp(a.started) && Date.parse(a.started) < Date.parse(a.live_since));
  return rows.length ? { started: Math.min(...rows.map(a => Date.parse(a.started))), live: Math.max(...rows.map(a => Date.parse(a.live_since))) } : null;
}
export function simBackfillNote(data) {
  const b = simLiveSince(data);
  return b ? `${clock.format(b.started)} 起 · ${clock.format(b.live)} 之前为历史补跑（同一套规则按小时重放），之后为实时运行` : '';
}

// ---- OKX demo-trading mirror (tools/okx_demo_trader.py) -------------------------------------
export const OKX_DEMO_URL = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/okx_demo/summary.json';
const ACTIONS = { open: '开仓', close: '平仓', amend_stop: '上移止损', closed_on_exchange: '交易所止损 / 止盈触发', skip: '跳过', daily_halt: '日亏停手',
  pause: '暂停跟单', resume: '恢复跟单', flatten: '全部平仓并暂停', error: '出错' };
const SKIPS = { paper_entry_too_old: '模拟盘开仓已超过 2 小时', stop_too_close_or_crossed: '价格已接近或越过止损', target_already_reached: '价格已到止盈',
  size_below_minimum: '仓位小于最小下单量', insufficient_margin: '保证金不足', exchange_position_exists_not_ours: '交易所已有非本程序的仓位',
  no_price: '取不到价格', no_instrument_spec: '取不到合约信息' };

export function validOkxDemo(d) {
  return d !== null && typeof d === 'object' && stamp(d.updated_at) && finite(d.equity) && Array.isArray(d.positions) &&
    d.positions.every(p => symbol(p.inst)) && Array.isArray(d.recent_actions || []);
}

export function okxDemoStatus(d, now = Date.now()) {
  if (now - Date.parse(d.updated_at) > SIM_STALE) return { label: '数据延迟', cls: 'down' };
  if (d.halted) return { label: '日亏停手', cls: 'down' };
  if (d.paused) return { label: '已暂停', cls: 'muted' };
  return { label: '运行中', cls: 'up' };
}

const px = v => fmt(v, Math.abs(v) >= 100 ? 1 : Math.abs(v) >= 1 ? 3 : 5);
export function okxDemoMarkup(d, now = Date.now()) {
  const st = okxDemoStatus(d, now), num = v => Number(v);
  const day = finite(d.day_start) && d.day_start > 0 ? d.equity - d.day_start : null;
  const pos = d.positions.length ? `<div class="okx-scroll"><table class="okx-positions"><thead><tr><th>币</th><th>方向</th><th>张数</th><th>均价</th><th>标记价</th><th>浮动盈亏</th><th>杠杆</th></tr></thead><tbody>${d.positions.map(p => {
      const side = p.side === 'long' ? '多' : p.side === 'short' ? '空' : num(p.contracts) > 0 ? '多' : '空';
      return `<tr><td>${coinIcon(p.inst)}${esc(p.inst.split('-')[0])}${p.ours ? '' : ' <small class="muted">(非跟单)</small>'}</td><td class="${side === '多' ? 'up' : 'down'}">${side}</td><td>${esc(Math.abs(num(p.contracts)))}</td><td>${px(num(p.avgPx))}</td><td>${px(num(p.markPx))}</td><td class="${tone(num(p.upl))}">${signed(num(p.upl))}</td><td>${esc(p.lever)}×</td></tr>`;
    }).join('')}</tbody></table></div>` : '<p class="empty">交易所暂无持仓</p>';
  const acts = (d.recent_actions || []).slice(0, 8).map(a => `<li><div><span>${esc(ACTIONS[a.action] || a.action)}${a.inst && a.inst !== '-' ? ' · ' + esc(a.inst.split('-')[0]) : ''}${a.dry_run ? ' <small class="muted">(空跑)</small>' : ''}</span><time>${time(a.ts)}</time></div><div><small class="muted">${esc(a.action === 'skip' ? SKIPS[a.reason] || a.reason : a.action === 'open' ? `${a.side === 1 ? '多' : '空'} ${a.contracts} 张 @ ${a.price} · 止损 ${a.stop}${a.target ? ' · 止盈 ' + a.target : ''} · ${a.leverage}×` : a.action === 'amend_stop' ? `${a.old} → ${a.new}` : a.action === 'error' ? a.detail || a.step : a.reason || '')}</small></div></li>`).join('');
  return `<div class="okx-head"><span class="pill ${st.cls}">${st.label}</span><small class="muted">更新 ${time(d.updated_at)} UTC+7 · 模拟资金，不涉及真实账户</small></div>` +
    `<div class="history-metrics">${[['权益 USDT', fmt(d.equity)], ['今日盈亏', day == null ? '—' : `${signed(day)} USDT`, tone(day)], ['可用 USDT', fmt(d.available)], ['持仓', `${d.positions.length}`]]
      .map(([k, v, c = '']) => `<div><span>${k}</span><strong class="${c}">${v}</strong></div>`).join('')}</div>${pos}` +
    `<h3 class="okx-subtitle">最近跟单动作</h3><ol class="fills-list">${acts || '<li class="empty">还没有跟单动作</li>'}</ol>`;
}

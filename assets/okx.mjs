// OKX demo-trading account (tools/okx_demo_trader.py) published on the paper-data branch.
// The strategy engine (tools/paper_trader.py) still runs in the background and supplies the signals;
// the dashboard shows only the exchange account.
import { coinIcon } from './coins.mjs';

export const OKX_URL = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/okx_demo/summary.json';
export const OKX_STALE = 6 * 3600000;        // runs every 4 hours; GitHub cron can lag by an hour or more
export const OKX_SNAPSHOT_DELAY = 5 * 3600000;
const clockPP = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Phnom_Penh', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
export function nextPaperUpdate(now = Date.now()) {
  const d = new Date(now), hour = Math.floor(d.getUTCHours() / 4) * 4;
  let cand = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hour, 17, 0, 0);
  if (cand <= now) cand = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), hour + 4, 17, 0, 0);
  return cand;
}
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
// Prices at exchange precision: about five significant digits (BTC 70,000.1 · XRP 1.2595 · DOGE 0.18123), no padding zeros.
const px = v => finite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: Math.min(8, Math.max(2, 4 - Math.floor(Math.log10(Math.abs(v) || 1)))) }) : '—';
const coin = inst => String(inst).split('-')[0];
const STRATEGIES = { mean_reversion: '均值回归', breakout_long: '趋势突破只做多', breakout_daily: '日线趋势突破', execution_test: '模拟成交测试' , evolved: '进化冠军' };
const ACTIONS = { open: '开仓', close: '平仓', amend_stop: '上移止损', closed_on_exchange: '交易所止损 / 止盈触发', skip: '跳过', daily_halt: '日亏停手',
  restore_stop: '补挂止损', pause: '暂停跟单', resume: '恢复跟单', follow_current: '手动跟进当前持仓', flatten: '全部平仓并暂停', error: '出错' };
const SKIPS = { paper_entry_too_old: '信号太旧，价格已离开入场区间', stop_too_close_or_crossed: '价格已接近或越过止损', target_already_reached: '价格已到止盈',
  size_below_minimum: '仓位小于最小下单量', insufficient_margin: '保证金不足', exchange_position_exists_not_ours: '交易所已有非本程序的仓位',
  no_price: '取不到价格', demo_price_off: '模拟盘价格偏离真实行情超过 1%', no_instrument_spec: '取不到合约信息', same_side_risk: '达到同向风险上限', same_side_count: '同方向持仓数已达上限', existing_risk_unknown: '现有仓位风险暂时无法确认' };
const CLOSES = { stop_missing_price_beyond_stop: '止损单缺失且价格已越过止损，直接平仓', paper_closed: '策略信号平仓', daily_halt: '日亏停手平仓', manual_flatten: '手动全部平仓', execution_test: '模拟成交测试 · 成交后立即平仓' };

export function validOkx(d) {
  return d !== null && typeof d === 'object' && stamp(d.updated_at) && finite(d.equity) && Array.isArray(d.positions) &&
    d.positions.every(p => p && symbol(p.inst)) && Array.isArray(d.recent_actions || []) &&
    (d.history === undefined || Array.isArray(d.history) && d.history.every(p => Array.isArray(p) && finite(p[0]) && finite(p[1])));
}

const sideOf = p => p.side === 'long' ? 1 : p.side === 'short' ? -1 : num(p.contracts) >= 0 ? 1 : -1;
export const okxStart = d => finite(num(d?.start_equity)) && d.start_equity > 0 ? Number(d.start_equity) : d?.history?.[0]?.[1] > 0 ? d.history[0][1] : DEFAULT_START;

// OKX demo trading has its own markets: its prices differ from the live exchange (XRP was 1.2686 on demo vs 1.2811 live).
// Floating PnL therefore uses demo mark prices (marks: Map inst -> {price, at}, from the demo public WebSocket), which is
// also what OKX itself uses for unrealised PnL; without a fresh demo mark it shows the values recorded on the last run.
export const OKX_DEMO_PUBLIC_WS = 'wss://wspap.okx.com/ws/v5/public';
export const OKX_DEMO_FALLBACK_WS = 'wss://wspap.okx.com:8443/ws/v5/public';
const MARK_FRESH = 30000;
export function okxLive(d, marks = new Map(), now = Date.now()) {
  const positions = d.positions.map(p => {
    const m = marks.get(p.inst), mark = m && now - m.at <= MARK_FRESH ? m.price : 0, ct = num(p.ctVal), qty = Math.abs(num(p.contracts)), entry = num(p.avgPx);
    if (mark > 0 && ct > 0 && qty > 0 && entry > 0) return { ...p, side: sideOf(p), mark, upl: sideOf(p) * qty * ct * (mark - entry), live: true };
    return { ...p, side: sideOf(p), mark: num(p.markPx), upl: num(p.upl), live: false };
  });
  const recorded = d.positions.reduce((s, p) => s + (finite(num(p.upl)) ? num(p.upl) : 0), 0);
  const live = positions.length > 0 && positions.every(p => p.live);
  return { equity: live ? d.equity - recorded + positions.reduce((s, p) => s + p.upl, 0) : d.equity, positions, live };
}

// One closed trade that made more than 20 % of the starting equity: the headline return is mostly that trade, say so.
export function okxOutlier(d, t) {
  const x = d?.largest_close, start = okxStart(d);
  if (!x || !finite(x.pnl) || !(x.pnl > start * .2) || !t) return null;
  return { inst: x.inst, pnl: x.pnl, ts: x.ts, retWithout: ((t.equity - x.pnl) / start - 1) * 100 };
}
export const okxOutlierNote = o => o ? `其中 ${signed(o.pnl)} USDT 来自 ${time(o.ts).slice(0, 5)} 一笔 ${esc(coin(o.inst))}（单笔占比过大，不代表策略水平）；扣除后 <span class="${o.retWithout > 0 ? 'up' : 'down'}">${signed(o.retWithout)}%</span>` : '';

// Paper book (evolution team) against what the OKX mirror actually holds, coin by coin.
export function mirrorCompareMarkup(d, evo) {
  if (!d || !evo?.paper) return '<p class="empty">等待纸面账户与 OKX 账户数据</p>';
  const paper = new Map((evo.paper.positions || []).map(p => [p.inst, p]));
  const ours = new Map(d.positions.filter(p => p.ours !== false).map(p => [p.inst, p]));
  const lastSkip = new Map();
  for (const a of d.recent_actions || []) if (a.action === 'skip' && a.inst) lastSkip.set(a.inst, a.reason);
  const side = v => v === 1 || v === 'long' ? '<b class="up">多</b>' : '<b class="down">空</b>';
  const rows = [...new Set([...paper.keys(), ...ours.keys()])].map(inst => {
    const p = paper.get(inst), o = ours.get(inst);
    const state = p && o ? '<span class="up">已跟上</span>' : p ? `<span class="down">没跟上</span> · ${esc(SKIPS[lastSkip.get(inst)] || '等下一次运行')}` : '纸面已平，下次运行平仓';
    return `<li><span>${coinIcon(inst)}<b>${esc(coin(inst))}</b> · 纸面 ${p ? side(p.side) : '—'} · OKX ${o ? side(o.side) : '—'}</span><span>${state}</span></li>`;
  }).join('');
  const both = [...paper.keys()].filter(k => ours.has(k)).length;
  return `<p class="muted">纸面持仓 <b>${paper.size}</b> 个，OKX 跟上 <b>${both}</b> 个${ours.size > both ? `，另有 ${ours.size - both} 个等待平仓` : ''}。</p>` +
    `<ul class="evo-list">${rows || '<li class="empty">两边都没有持仓</li>'}</ul>`;
}

// Return per day (UTC+7 days, the account's own day): each day's last equity against the previous day's last
// (the first day against its first point). Today ends at the live equity. Days without a run are absent, so the
// first day after a gap carries the whole gap. Newest first.
const dayKey = t => new Date(t + 7 * 3600e3).toISOString().slice(0, 10);
export function okxDailyReturns(d, liveEquity = null, now = Date.now()) {
  const pts = (d?.history || []).filter(p => finite(p[0]) && finite(p[1]) && p[1] > 0).sort((a, b) => a[0] - b[0]);
  if (!pts.length) return [];
  const close = new Map();
  for (const [t, e] of pts) close.set(dayKey(t), e);
  if (finite(liveEquity) && liveEquity > 0) close.set(dayKey(now), liveEquity);
  const days = [...close.keys()].sort(), out = [], today = dayKey(now), dayStart = num(d.day_start);
  let prev = pts[0][1], prevDay = null;
  for (const k of days) {
    const e = close.get(k);
    if (k === today && dayStart > 0) prev = dayStart;            // today: from the account's own start-of-day equity, as the header shows
    const gap = prevDay ? Math.round((Date.parse(k) - Date.parse(prevDay)) / 864e5) - 1 : 0;
    out.push({ day: k, usdt: e - prev, pct: (e / prev - 1) * 100, equity: e, gap });
    prev = e; prevDay = k;
  }
  return out.reverse();
}

export function okxDailyMarkup(rows, limit = 10) {
  if (!rows.length) return '<p class="empty">跑满一天后这里会出现每天的收益</p>';
  const shown = rows.slice(0, limit), top = Math.max(...shown.map(r => Math.abs(r.pct)), .01);
  const md = k => `${k.slice(5, 7)}/${k.slice(8, 10)}`;
  return `<ol class="okx-daily">${shown.map((r, i) => {
    const w = Math.max(2, Math.abs(r.pct) / top * 100).toFixed(1), cls = r.pct >= 0 ? 'up' : 'down';
    return `<li><span class="okx-daily-day">${i === 0 ? '今天' : md(r.day)}${r.gap > 0 ? `<small>含前 ${r.gap} 天</small>` : ''}</span>` +
      `<span class="okx-daily-bar ${cls}"><i style="width:${w}%"></i></span>` +
      `<span class="okx-daily-val"><b class="${cls}">${signed(r.pct)}%</b><small>${signed(r.usdt)} U</small></span></li>`;
  }).join('')}</ol>`;
}

export function okxTotals(d, marks = new Map(), now = Date.now()) {
  if (!d) return null;
  const l = okxLive(d, marks, now), start = okxStart(d);
  const dayStart = num(d.day_start), day = dayStart > 0 ? l.equity - dayStart : null;
  return { equity: l.equity, start, ret: (l.equity / start - 1) * 100, day, dayPct: day == null ? null : day / dayStart * 100,
    positions: d.positions.length, trades: finite(num(d.closed_trades)) ? Number(d.closed_trades) : 0, live: l.live };
}

export function okxStatus(d, now = Date.now()) {
  if (d.halted) return { label: '日亏停手', cls: 'down' };
  if (d.paused) return { label: '已暂停', cls: 'muted' };
  if (now - Date.parse(d.updated_at) > OKX_SNAPSHOT_DELAY) return { label: '快照延迟 · 未暂停', cls: 'down' };
  return { label: '允许开仓', cls: 'up' };
}

export function demoQuoteState(d,marks=new Map(),stream=null,pollAt=0,now=Date.now()) {
  const symbols=[...new Set((d?.positions||[]).map(p=>p.inst))];
  if(!symbols.length)return {label:'账户快照',live:false,polling:false,needsQuotes:false};
  const fresh=symbols.every(id=>{const m=marks.get(id);return m?.price>0&&now-m.at>=0&&now-m.at<=30000;});
  const live=fresh&&stream?.state==='实时'&&now-stream.lastData<45000;
  const polling=fresh&&!live&&pollAt>0&&now-pollAt<24000;
  return {label:live?'行情实时':polling?'行情轮询':'行情断开',live,polling,freshMarks:fresh,needsQuotes:true};
}

export function accountDataNote(d,quote,now=Date.now()) {
  if(!d)return '账户快照尚未读取。';
  const age=Math.max(0,now-Date.parse(d.updated_at));
  const elapsed=age<60000?'不足 1 分钟':`${Math.floor(age/60000)} 分钟`;
  const last=clockPP.format(Date.parse(d.updated_at)), next=clockPP.format(nextPaperUpdate(now));
  const title=`上次更新 ${last} · 下次更新 ${next}（金边时间） · 距今 ${elapsed}`;
  const pricing=quote.live||quote.polling?'持仓按模拟行情估值；成交与余额仍以账户快照为准。':quote.freshMarks?'行情连接已断，暂用最近收到的模拟标记价估值。':quote.needsQuotes?'行情未连接，账户权益使用上次快照值；持仓报价以各行标注为准。':'当前无持仓，无需持仓行情订阅。';
  return `${title}。${pricing}${age>OKX_SNAPSHOT_DELAY?' 已超过 5 小时没有更新，定时任务可能延迟。':''}`;
}

export function okxCurveSVG(d) {
  const pts = (d.history || []).slice().sort((a, b) => a[0] - b[0]);
  if (pts.length < 2 || pts.at(-1)[0] === pts[0][0]) return '<p class="empty">资金曲线会在跟单运行几次后出现（每 15 分钟一个点）</p>';
  const base = okxStart(d), t0 = pts[0][0], t1 = pts.at(-1)[0], vals = pts.map(p => p[1]);
  const lo = Math.min(...vals, base), hi = Math.max(...vals, base), pad = Math.max((hi - lo) * .12, base * .002);
  const W = 680, H = 250, L = 118, R = 12, T = 16, B = 40, x = t => L + (t - t0) / (t1 - t0) * (W - L - R), y = v => T + (hi + pad - v) / (hi - lo + pad * 2) * (H - T - B);
  const grid = [hi + pad, base, lo - pad].map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="${v === base ? 'sim-base' : 'equity-grid'}"/><text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${fmt(v)}</text>`).join('');
  // Coloured like Apple's Stocks: green when the account is above where the curve starts, red when below; a soft area under it.
  const dir = vals.at(-1) >= vals[0] ? 'curve-up' : 'curve-down', line = pts.map(p => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ');
  const floor = (H - B).toFixed(1), tip = `<circle class="curve-tip ${dir}" cx="${x(t1).toFixed(1)}" cy="${y(vals.at(-1)).toFixed(1)}" r="4.5"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="OKX 模拟账户资金曲线">${grid}<polygon class="curve-area ${dir}" points="${x(t0).toFixed(1)},${floor} ${line} ${x(t1).toFixed(1)},${floor}"/>` +
    `<polyline class="curve-line ${dir}" points="${line}" fill="none" stroke="${OKX_COLOR}" stroke-width="2" vector-effect="non-scaling-stroke"/>${tip}` +
    `<text x="${L}" y="${H - 8}">${time(t0)}</text><text x="${W - R}" y="${H - 8}" text-anchor="end">${time(t1)}</text></svg>`;
}

// Compact position rows: coin, side, leverage and floating PnL stay visible; tap to expand the details.
export function okxPositionsMarkup(d, marks = new Map(), now = Date.now(), { open = false, owners = null } = {}) {   // owners: {inst: {champion, boost}} from the paper account
  const { positions } = okxLive(d, marks, now);
  if (!positions.length) return '<p class="empty">当前没有持仓，等待策略信号</p>';
  const mirrors = Object.fromEntries((d.open_mirrors || []).map(m => [m.inst, m]));
  return positions.map(p => {
    const m = mirrors[p.inst], margin = num(p.margin), pct = margin > 0 && finite(p.upl) ? p.upl / margin * 100 : null;
    const own = m?.champion ? { champion: m.champion, boost: m.leader } : owners?.[p.inst];
    const tag = !p.ours ? '非本程序仓位' : own?.champion ? `${esc(own.champion)}${own.boost ? ' · <b>第一名</b>' : ''}` : esc(STRATEGIES[m?.strategy] || '跟单');
    const pnl = finite(p.upl) ? `${signed(p.upl)} USDT` : '—';
    return `<details class="okx-pos sim-position${p.ours ? '' : ' manual'}" data-inst="${esc(p.inst)}"${open ? ' open' : ''}><summary>` +
      `<span class="okx-pos-coin">${coinIcon(p.inst)}<b>${esc(coin(p.inst))}</b><i class="okx-side ${p.side === 1 ? 'up' : 'down'}">${p.side === 1 ? '多' : '空'} ${esc(p.lever)}×</i></span>` +
      `<span class="okx-pos-tag">${tag}</span>` +
      `<span class="okx-pos-pnl ${tone(p.upl)}"><b>${pnl}</b><small>${pct == null ? '' : `${signed(pct)}%`}</small></span></summary>` +
      `<div class="history-metrics okx-pos-body">` +
      [['张数', esc(Math.abs(num(p.contracts)))], ['开仓均价', px(num(p.avgPx))], [p.live ? '标记价（实时）' : `标记价（${time(d.updated_at)}）`, px(p.mark)],
       ['止损', m && finite(num(m.stop)) ? px(num(m.stop)) : '—'], ['保证金 USDT', fmt(margin)], ['收益率（按保证金）', pct == null ? '—' : `${signed(pct)}%`, tone(p.upl)]]
        .map(([k, v, c = '']) => `<div><span>${k}</span><strong class="${c}">${v}</strong></div>`).join('') + '</div></details>';
  }).join('');
}

// Exchange-side closes carry the realised result from OKX position history (pnl incl. fees, close price, outcome).
const OUTCOME = { stop: '止损', target: '止盈', liquidation: '强平' };
const closeTitle = a => a.action === 'closed_on_exchange' ? (OUTCOME[a.outcome] ? `交易所${OUTCOME[a.outcome]}触发` : '交易所止损 / 止盈触发') : (ACTIONS[a.action] || a.action);
// Realised = price PnL + fees + funding (OKX position history); the parts explain e.g. a target fill that still lost money.
const realised = a => {
  if (!finite(num(a.pnl))) return '';
  const parts = [['价差', num(a.price_pnl)], ['手续费', num(a.fee)], ['资金费', num(a.funding)]].filter(([, v]) => finite(v) && Math.abs(v) >= .005);
  return `已实现 ${signed(num(a.pnl))} USDT${parts.length ? `（${parts.map(([k, v]) => `${k} ${signed(v)}`).join('，')}）` : '（含手续费）'}`;
};
function actionDetail(a) {
  if (a.action === 'skip') return SKIPS[a.reason] || a.reason || '';
  if (a.action === 'open') return `${STRATEGIES[a.strategy] ? STRATEGIES[a.strategy] + ' · ' : ''}${a.side === 1 ? '多' : '空'} ${a.contracts} 张 @ ${a.price} · 止损 ${a.stop}${a.target ? ' · 止盈 ' + a.target : ''} · ${a.leverage}×`;
  if (a.action === 'amend_stop') return `止损 ${px(num(a.old))} → ${px(num(a.new))}`;
  if (a.action === 'restore_stop') return `持仓在交易所没有止损单，已按 ${a.stop} 补挂`;
  if (a.action === 'close') return a.execution_test ? `${CLOSES.execution_test} · ${a.contracts} 张 @ ${a.price} · 净损益 ${signed(num(a.net_pnl), 4)} USDT（含手续费）` : [CLOSES[a.reason] || a.reason || '', finite(num(a.close_price)) ? `平仓价 ${px(num(a.close_price))}` : '', realised(a)].filter(Boolean).join(' · ');
  if (a.action === 'closed_on_exchange') return [finite(num(a.close_price)) ? `成交价 ${px(num(a.close_price))}` : '', realised(a) || '金额待下次运行从交易所补全'].filter(Boolean).join(' · ');
  if (a.action === 'daily_halt') { const loss = num(a.day_start) > 0 ? (1 - num(a.equity) / num(a.day_start)) * 100 : null;
    return `当日亏损${finite(loss) ? ` ${fmt(loss, 1)}%` : ''}达到停手线（10%），全部平仓，次日恢复`; }
  if (a.action === 'error') return a.detail || a.step || '';
  return '';
}

// Fills and risk events versus housekeeping (pause / resume / skips), so real trades are not buried.
const TRADE_ACTIONS = new Set(['open', 'close', 'closed_on_exchange', 'restore_stop', 'daily_halt', 'error']);   // stop moves are housekeeping: 运行记录
const ACTION_TONE = { open: 'open', close: 'close', closed_on_exchange: 'stop', amend_stop: 'info', restore_stop: 'info', daily_halt: 'stop', error: 'stop' };
const toneOf = a => a.action === 'closed_on_exchange' && finite(num(a.pnl)) ? (num(a.pnl) >= 0 ? 'close' : 'stop') : ACTION_TONE[a.action] || 'muted';
export const isTradeAction = a => TRADE_ACTIONS.has(a.action);

// kind: 'all' (default, every action), 'trades' (fills and risk events) or 'system' (housekeeping).
export function okxActionsMarkup(d, limit = 10, { kind = 'all' } = {}) {
  const pick = kind === 'trades' ? isTradeAction : kind === 'system' ? a => !isTradeAction(a) : () => true;
  const rows = (d.recent_actions || []).filter(pick).slice(0, limit);
  if (!rows.length) return `<li class="empty">${kind === 'system' ? '暂无运行记录' : kind === 'trades' ? '还没有成交，等待策略信号' : '还没有交易动作'}</li>`;
  return rows.map(a => `<li class="okx-act" data-tone="${toneOf(a)}"><div><span><i class="okx-act-dot" aria-hidden="true"></i><b>${esc(closeTitle(a))}${a.inst && a.inst !== '-' ? ' · ' + esc(coin(a.inst)) : ''}</b>${a.dry_run ? ' <small class="muted">(空跑)</small>' : ''}</span><time>${time(a.ts)}</time></div>${actionDetail(a) ? `<p>${esc(actionDetail(a))}</p>` : ''}</li>`).join('');
}

const feedLine = a => a.action === 'open' ? { text: `开仓 ${coin(a.inst)} ${a.side === 1 ? '多' : '空'} ${a.contracts} 张 @ ${a.price}`, mood: 'excited' }
  : a.action === 'close' ? { text: `平仓 ${coin(a.inst)} · ${CLOSES[a.reason] || '策略信号'}${finite(num(a.pnl)) ? ` · ${signed(num(a.pnl))} USDT` : ''}`, mood: finite(num(a.pnl)) && num(a.pnl) < 0 ? 'sad' : 'happy' }
  : a.action === 'closed_on_exchange' ? { text: `${coin(a.inst)} ${closeTitle(a)}${finite(num(a.pnl)) ? ` · ${signed(num(a.pnl))} USDT` : ''}`, mood: finite(num(a.pnl)) && num(a.pnl) >= 0 ? 'happy' : 'sad' }
  : a.action === 'daily_halt' ? { text: '触发日亏停手，全部平仓，今天不再开新仓', mood: 'sad' }
  : a.action === 'error' ? { text: `跟单出错${a.inst && a.inst !== '-' ? ' · ' + coin(a.inst) : ''}`, mood: 'sad' } : null;
const actionKey = a => `${a.ts}|${a.action}|${a.inst}`;

export function okxFeed(d, limit = 20) {
  return (d?.recent_actions || []).map(a => ({ ts: Date.parse(a.ts), ...feedLine(a) })).filter(r => r.text && finite(r.ts)).slice(0, limit);
}

// Actions that appeared since the previous summary, oldest first, for toasts and the companion.
// Raw actions that appeared since the previous summary, oldest first (for the companion's trade tickets).
export function okxNewActions(prev, next) {
  if (!prev || !next) return [];
  const seen = new Set((prev.recent_actions || []).map(actionKey));
  return (next.recent_actions || []).filter(a => !seen.has(actionKey(a))).reverse();
}

export function okxChanges(prev, next) {
  if (!prev || !next) return [];
  const seen = new Set((prev.recent_actions || []).map(actionKey));
  return (next.recent_actions || []).filter(a => !seen.has(actionKey(a))).reverse().map(feedLine).filter(Boolean);
}

export function okxMascotState(d, now = Date.now()) {
  if (!d || now - Date.parse(d.updated_at) > OKX_STALE) return { state: 'offline', label: '离线', message: 'OKX 模拟账户超过 6 小时没有更新，我在等下一次运行' };
  if (d.halted) return { state: 'paused', label: '停手', message: '今日触发日亏停手，不再开新仓' };
  if (d.paused) return { state: 'paused', label: '暂停', message: '跟单已暂停，恢复后我会播报开平仓' };
  const recent = (d.recent_actions || []).filter(a => finite(Date.parse(a.ts)) && now - Date.parse(a.ts) >= 0 && now - Date.parse(a.ts) <= RECENT);
  const latest = recent.find(a => ['open', 'close', 'closed_on_exchange'].includes(a.action));
  if (latest?.action === 'closed_on_exchange') return { state: 'sad', label: '刚止损 / 止盈', message: `${coin(latest.inst)} 交易所止损 / 止盈触发` };
  if (latest?.action === 'close') return latest.execution_test
    ? { state: 'idle', label: '测试完成', message: `${coin(latest.inst)} 模拟成交测试已平仓` }
    : { state: 'idle', label: '刚平仓', message: `${coin(latest.inst)} 已平仓 · ${CLOSES[latest.reason] || '查看交易回执'}` };
  if (latest?.action === 'open') return { state: 'excited', label: '刚开仓', message: `刚开仓 ${coin(latest.inst)} ${latest.side === 1 ? '多' : '空'}` };
  const t = okxTotals(d);
  if (t.ret > 0) return { state: 'happy', label: '盈利中', message: `OKX 模拟账户累计 ${signed(t.ret)}%` };
  return { state: 'idle', label: '待命', message: t.positions ? `持仓 ${t.positions} 个，止损挂在交易所` : '空仓中，等待新信号' };
}

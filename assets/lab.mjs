// 进化回放 page: the rolling-evolution replay account exported from modi-cjh/trading-evo (trading/ga/export_panel.py)
// into lab/account.json. A separate system from 策略进化 (tools/evolve); nothing here trades. Pure functions (unit-tested).
export const LAB_URL = './lab/account.json';
const finite = Number.isFinite;
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = (v, d = 2) => finite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—';
const signed = (v, d = 2) => finite(v) ? (v > 0 ? '+' : '') + fmt(v, d) : '—';
const tone = v => finite(v) ? v >= 0 ? 'up' : 'down' : '';
const px = v => finite(v) ? String(Number(v.toPrecision(6))) : '—';
const H = 3600e3;
// The replay's times are UTC without a zone ("2026-11-07T21:00"); the panel shows UTC+7.
const utc = s => Date.parse(/Z$|[+-]\d\d:?\d\d$/.test(s) ? s : s + (s.length <= 13 ? ':00Z' : 'Z'));
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const day = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', year: 'numeric', month: '2-digit', day: '2-digit' });
export const labTime = s => finite(utc(String(s))) ? clock.format(utc(String(s))) : '—';
const labDate = s => finite(utc(String(s))) ? day.format(utc(String(s))) : '—';
export const SPECIES = { vol_breakout: '波动突破', ma_cross: '均线趋势', rsi_revert: '均值回归', channel: '通道突破', vol_surge: '放量异动', rel_strength: '相对强弱' };

export function validLab(d) {
  return Boolean(d) && typeof d === 'object' && d.schema === 1 && finite(d.start_equity) && finite(d.bar_hours) && d.bar_hours > 0 &&
    finite(utc(String(d.start))) && Array.isArray(d.ohlc) && d.ohlc.length > 0 &&
    d.ohlc.every(r => Array.isArray(r) && r.length === 4 && r.every(finite)) &&
    Array.isArray(d.strategies) && Array.isArray(d.positions) && Array.isArray(d.closed);
}

// Equity candles: the file has one OHLC row per bar_hours from start; "day" / "week" merge them (UTC days, weeks from Monday).
export function labBars(d, period = 'day') {
  const t0 = utc(String(d.start)), step = d.bar_hours * H;
  const raw = d.ohlc.map(([open, high, low, close], i) => ({ t: t0 + i * step, open, high, low, close }));
  if (period === '4h') return raw.map(({ t, ...b }) => ({ time: t / 1000, ...b }));
  const size = period === 'week' ? 7 * 24 * H : 24 * H, offset = period === 'week' ? 4 * 24 * H : 0;   // 1970-01-01 was a Thursday
  const out = [];
  for (const b of raw) {
    const key = Math.floor((b.t - offset) / size) * size + offset, last = out.at(-1);
    if (last && last.time === key / 1000) { last.high = Math.max(last.high, b.high); last.low = Math.min(last.low, b.low); last.close = b.close; }
    else out.push({ time: key / 1000, open: b.open, high: b.high, low: b.low, close: b.close });
  }
  return out;
}

export function labStats(d) {
  const closes = d.ohlc.map(r => r[3]), equity = closes.at(-1);
  let peak = d.start_equity, maxDd = 0;
  for (const c of closes) { peak = Math.max(peak, c); maxDd = Math.max(maxDd, peak > 0 ? 1 - c / peak : 0); }
  const perDay = Math.max(1, Math.round(24 / d.bar_hours)), dayAgo = closes.at(-1 - perDay) ?? d.start_equity;
  return { equity, pnl: equity - d.start_equity, ret: (equity / d.start_equity - 1) * 100, peak: Math.max(...closes, d.start_equity), maxDd: maxDd * 100, day: equity - dayAgo,
    floating: d.positions.reduce((s, p) => s + (finite(p.float) ? p.float : 0), 0) };
}

const metric = (k, v, c = '') => `<div><span>${k}</span><strong class="${c}">${v}</strong></div>`;

export function heroMarkup(d) {
  const s = labStats(d), m = d.margin, cs = d.closed_stats || {};
  return `<div class="equity-number"><strong>${fmt(s.equity)}</strong><small>USDT</small></div>` +
    `<p class="okx-ret ${tone(s.pnl)}">累计 ${signed(s.pnl)} USDT（${signed(s.ret)}%）· 起始 ${fmt(d.start_equity, 0)} USDT · ${labDate(d.start)} 起</p>` +
    `<div class="history-metrics">` +
    metric('最大回撤', `${fmt(s.maxDd, 1)}%`, 'down') + metric('最高权益', fmt(s.peak)) +
    metric('近 24 小时', signed(s.day), tone(s.day)) + metric('持仓浮盈', signed(s.floating), tone(s.floating)) +
    metric('日亏停手', `${d.rules?.stop_count ?? '—'} 次`) +
    metric('平仓笔数 / 胜率', finite(cs.n) && cs.n ? `${cs.n} / ${fmt(cs.wins / cs.n * 100, 0)}%` : '—') +
    (m ? metric('保证金占用 平均 / 最高', `${fmt(m.avg, 1)}% / ${fmt(m.max, 1)}%`) + metric('同时持仓 平均 / 最多', `${fmt(m.pos_avg, 1)} / ${esc(m.pos_max)}`) : '') +
    `</div>`;
}

export function strategiesMarkup(d) {
  const act = d.strategies.filter(s => s.active), off = d.strategies.filter(s => !s.active);
  const offSum = off.reduce((s, x) => s + (finite(x.pnl) ? x.pnl : 0), 0);
  const rows = act.map(s => `<li class="okx-act" data-tone="${s.pnl >= 0 ? 'open' : 'stop'}"><div><span><i class="okx-act-dot" aria-hidden="true"></i><b>${esc(s.id)} · ${esc(SPECIES[s.species] || s.species)} · ${esc(s.tf_name)}</b></span><b class="${tone(s.pnl)}">${signed(s.pnl)}</b></div><p>${esc(s.desc)}</p></li>`);
  if (off.length) rows.push(`<li class="okx-act" data-tone="info"><div><span><i class="okx-act-dot" aria-hidden="true"></i><b>已下线的 ${off.length} 个策略合计</b></span><b class="${tone(offSum)}">${signed(offSum)}</b></div></li>`);
  return rows.join('') || '<li class="empty">暂无上线策略</li>';
}

export function positionsMarkup(d) {
  if (!d.positions.length) return '<p class="empty">当前没有持仓</p>';
  return d.positions.map(p => {
    const pct = finite(p.float) && finite(p.margin) && p.margin > 0 ? p.float / p.margin * 100 : null;
    return `<details class="okx-pos"><summary><span class="okx-pos-coin"><b>${esc(p.pair)}</b><i class="okx-side ${p.side === 1 ? 'up' : 'down'}">${p.side === 1 ? '多' : '空'} ${esc(p.lev)}×</i></span>` +
      `<span class="okx-pos-tag">${esc(p.sid)} · 名义 ${fmt(p.notional)}</span>` +
      `<span class="okx-pos-pnl ${tone(p.float)}"><b>${signed(p.float)}</b><small>${pct == null ? '' : `${signed(pct)}%`}</small></span></summary>` +
      `<div class="history-metrics okx-pos-body">` +
      metric('开仓策略', esc(p.sid)) + metric('开仓时间 UTC+7', labTime(p.since)) + metric('开仓价', px(p.entry)) + metric('最新收盘', px(p.last)) +
      metric('止损', px(p.stop)) + metric('止盈', p.tp == null ? '不设（移动止损）' : px(p.tp)) +
      metric('保证金 USDT', fmt(p.margin)) + metric('本单风险', `${fmt(p.risk, 1)}% 权益`) + `</div></details>`;
  }).join('');
}

export function closedMarkup(d, limit = 20) {
  const rows = d.closed.slice(0, limit);
  if (!rows.length) return '<li class="empty">暂无平仓记录</li>';
  return rows.map(t => `<li class="okx-act" data-tone="${t.pnl >= 0 ? 'close' : 'stop'}"><div><span><i class="okx-act-dot" aria-hidden="true"></i><b>${esc(t.reason || '平仓')} · ${esc(t.pair)} ${t.side === 1 ? '多' : '空'}</b> <b class="${tone(t.pnl)}">${signed(t.pnl)}</b></span><time>${labTime(t.tx)}</time></div>` +
    `<p>${esc(t.sid)} · 开 ${px(t.entry)} → 平 ${px(t.exit)} · 名义 ${fmt(t.notional)} · ${labTime(t.te)} 开仓</p></li>`).join('');
}

export function versionsMarkup(d) {
  const rows = (d.versions || []).map(v => `<tr${v.id === d.label ? ' class="active"' : ''}><td><b>${esc(v.id)}</b>${v.id === d.label ? ' <span class="pill">当前</span>' : ''}<br><small>${esc(v.desc)}</small></td>` +
    `<td class="${tone(v.final - d.start_equity)}">${fmt(v.final)}</td><td class="down">${fmt(v.dd, 1)}%</td><td>${esc(v.stops)}</td></tr>`).join('');
  return rows ? `<div class="table-scroll" tabindex="0" aria-label="各版本对比，可左右滑动"><table><thead><tr><th>版本</th><th>期末 USDT</th><th>最大回撤</th><th>日亏停手</th></tr></thead><tbody>${rows}</tbody></table></div>` : '';
}

export function labCaption(d) {
  const r = d.rules || {};
  const risk = r.risk_gene ? `每笔风险按策略自己的风险基因（不低于 ${fmt((r.min_risk ?? 0) * 100, 0)}%）` : `每笔风险 ${fmt((r.risk ?? 0) * 100, 0)}% 权益`;
  return `持续进化回放（另一套系统，与“策略进化”页无关）：从 ${labDate(d.start)} 起每周用当时已有的数据进化 5 代（共 ${esc(d.generation)} 代），` +
    `6 个物种、4 个周期，挑战者考核 28 天胜出才接管账户。规则：${esc(r.lev)}x 逐仓，${risk}` +
    `${r.risk_budget ? `，全部持仓合计风险 ≤ ${fmt(r.risk_budget * 100, 0)}%` : ''}，每个策略最多 ${esc(r.per_strategy)} 个持仓、同一个币不重复开，日亏 ${fmt((r.daily_stop ?? 0) * 100, 0)}% 全部平仓停手到次日。` +
    `历史 K 线回放，没有真实或模拟下单，数据截至 ${labTime(d.end)} UTC+7。`;
}

// Candlestick chart of account equity (DOM; needs LightweightCharts). Returns { update(bars), destroy() }.
export function mountLabChart(element) {
  const LC = globalThis.LightweightCharts;
  if (!LC || !element) return null;
  const f = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', month: '2-digit', day: '2-digit', hour12: false });
  const chart = LC.createChart(element, { width: element.clientWidth, height: element.clientHeight,
    layout: { background: { color: '#ffffff' }, textColor: '#4b5563', fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
    grid: { vertLines: { color: '#edf0f3' }, horzLines: { color: '#edf0f3' } }, rightPriceScale: { borderColor: '#d1d5db' },
    timeScale: { borderColor: '#d1d5db', tickMarkFormatter: t => f.format(new Date(Number(t) * 1000)) },
    localization: { locale: 'zh-CN', timeFormatter: t => clock.format(new Date(Number(t) * 1000)) }, handleScroll: { vertTouchDrag: false } });
  const series = chart.addCandlestickSeries({ upColor: '#087c50', downColor: '#c53045', borderVisible: false, wickUpColor: '#087c50', wickDownColor: '#c53045', priceLineVisible: true });
  let base = null;
  const observer = new ResizeObserver(() => chart.applyOptions({ width: element.clientWidth, height: element.clientHeight }));
  observer.observe(element);
  return {
    update(bars, startEquity) {
      series.setData(bars);
      if (!base && finite(startEquity)) base = series.createPriceLine({ price: startEquity, color: '#9aa4b2', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '本金' });
      chart.timeScale().fitContent();
    },
    destroy() { observer.disconnect(); chart.remove(); },
  };
}

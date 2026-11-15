// MODI dashboard: OKX live account (real money) + the 策略进化 page (paper-data/evolve, the strategy engine that feeds the signals). The account comes from the
// server's read-only API (tools/live_readapi.py, every minute), else the live-data branch (tools/live_sync.py, hourly); prices, marks and candles come from
// OKX public endpoints (www.okx.com, then aws.okx.com), with the server's cached tickers / candles as the last fallback. While the server's push
// (/stream, SSE) is up, position marks arrive about once a second and PnL / equity tick live; when it drops, the 60-second polling carries on.
import { coinIcon } from './coins.mjs';
import { Mascot } from './mascot.mjs';
import { okxDailyReturns, okxDailyMarkup } from './okx.mjs';
import { LIVE_SNAPSHOT_URL, LIVE_FILLS_URL, LIVE_TRADES_URL, LIVE_API_BASE, LIVE_API_URL, LIVE_TICKERS_URL, LIVE_STREAM_URL, API_FRESH, streamUpdate, splitLiveApi, serverTickerRows, sourceText, connectionBadge, validLiveSnapshot, validLiveFills, validLiveTrades, liveView, liveFreshness, liveStatus, liveCurveSVG, livePositionsMarkup, liveFillsMarkup, liveNewFills, liveFeed, liveMascotState,
  liveLight, liveLightMarkup, closedTradesMarkup, tradeStatsMarkup, chartSubject, candleRequest, levelsLadderSVG, tradeMarkers, levelColor, px as livePx } from './live.mjs?v=20261116-push';
import { PublicStream, ServerPush, mergeCandle } from './realtime.mjs?v=20261116-push';
import { configureMotion, rollNumber, toast } from './effects.mjs';
import { workState, pnlMood, tradeBurst, positionReport, pnlComment } from './robot-work.mjs';
import { dayNumber } from './account.mjs';
import config from '../config.js';
import { PublicAPI, OKX_REST_BASES, normalizeTickers, number, getCandles } from './api.mjs?v=20261116-push';
import { parseCandles, analyze } from './indicators.mjs';
import { chartAvailable, createCandleCharts, createTradeChart } from './charts.mjs?v=20261116-dash';
import { EVOLVE_URL, EVOLVE_NODES_URL, validNodeDetails, needsDetails, validEvolve, loopSVG, statsMarkup, fitnessSVG, graphSVG, detailMarkup, teamMarkup, homeCardMarkup, dataCaption, replayGenerations, replayState, replayDelays, updateSchedule, evolveIntroMarkup } from './evolve.mjs?v=20261115-evolve';
import { GeneFx } from './evolve-fx.mjs';

const $ = id => document.getElementById(id);
configureMotion();
const mascot = new Mascot($('mascot'), $('mascot-drawer'), { tabs: ['status', 'feed'] });
const BUILD = '2026-11-16 · OKX 实盘看板 · 服务器推送', diagLog = [];
const diagNote = text => { if (diagLog.length && diagLog.at(-1).endsWith(' ' + text)) return; diagLog.push(`${new Date().toISOString().slice(11, 19)}Z ${text}`); diagLog.splice(0, diagLog.length - 12); };
addEventListener('error', e => diagNote(`脚本错误：${e.message || e.type}${e.filename ? ' @ ' + e.filename.split('/').pop() + ':' + e.lineno : ''}`));
addEventListener('unhandledrejection', e => diagNote(`未处理的异常：${e.reason?.message || e.reason}`));
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const num = (value, digits = 2) => Number.isFinite(value) ? value.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
const price = value => num(value, value >= 100 ? 2 : value >= 1 ? 4 : value >= .01 ? 6 : 8);
const compact = value => !Number.isFinite(value) ? '—' : Math.abs(value) >= 1e8 ? num(value / 1e8) + ' 亿' : Math.abs(value) >= 1e4 ? num(value / 1e4) + ' 万' : num(value);
const signed = (value, digits = 2) => Number.isFinite(value) ? (value > 0 ? '+' : '') + num(value, digits) : '—';
const tone = value => Number.isFinite(value) ? value >= 0 ? 'up' : 'down' : '';
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const time = value => { const ms = typeof value === 'number' ? value : Date.parse(value); return Number.isFinite(ms) ? clock.format(ms) : '—'; };
// Write only when content changed: identical rewrites rebuild nodes and read as flicker.
const setHTML = (el, html) => { if (el && el.__html !== html) { el.innerHTML = html; el.__html = html; } };
const setText = (el, text) => { if (el && el.textContent !== text) el.textContent = text; };
const notify = line => { toast(line.text); mascot.say(line.text, line.mood); };

const api = new PublicAPI(), errors = new Map(), funding = new Map(), interests = new Map(), candleCache = new Map();
let tickers = [], top = [], priceAt = 0, slowAt = 0, priceBusy = false, slowBusy = false;
let route = 'home', coin = config.symbol, timeframe = '4H', routeVersion = 0, sortKey = 'turnover', sortDirection = -1;
let candleCharts = null, lastCandleRequest = '', candleBusy = new Set();
let live = null, fills = null, trades = null, liveBusy = false;
let liveSource = null, liveAccount = null, quoteSource = null;   // 'api' | 'github' ; 'okx' | 'server' (where the account / tickers came from)
const marks = new Map();   // inst -> {price, at}: OKX public mark prices for the live positions
let quoteStream = null, candleStream = null, quoteStreamKey = '', candleStreamKey = '';
let candleSource = 'okx', pushReadAt = 0;   // where the coin page's candles came from: 'okx' | 'server'
const candles = (path, params, priority) => getCandles(api, path, params, priority, { serverBase: LIVE_API_BASE });
// Server push: marks of the open positions about once a second; a newer account on the server triggers an immediate /live.json read.
const push = new ServerPush({ url: LIVE_STREAM_URL, staleMs: 25000,
  onState: (state, why) => { diagNote(`服务器推送：${state}${why ? ' · ' + why : ''}`); updateStreamStatus(); renderStatusPill(); },
  onMessage: body => {
    const u = streamUpdate(body, (live?.positions || []).map(p => p.inst)); if (!u) return false;
    const at = Date.now(); for (const [inst, value] of u.marks) marks.set(inst, { price: value, at });
    if (u.liveAt && (!live || Date.parse(u.liveAt) > Date.parse(live.updated_at)) && at - pushReadAt > 10000) { pushReadAt = at; refreshLive(); }
    if (u.marks.length) scheduleLiveRender();
    return true;
  } });

function setError(key, message) {
  if (message) errors.set(key, message); else errors.delete(key);
  renderStatusPill();
}
function description(error) { return /^(OKX|接口|请求)/.test(error.message) ? error.message : '网络连接失败或请求超时'; }
function badge(row) { return `${coinIcon(row.symbol)}<div><a href="#/coin/${encodeURIComponent(row.id)}">${escape(row.symbol)}</a><small>USDT 永续</small></div>`; }
function decorated() {
  return top.map(r => ({ ...r, funding: funding.get(r.id)?.rate ?? null, oi: Number.isFinite(interests.get(r.id)?.qty) ? interests.get(r.id).qty * r.last : null }));
}
function renderMarket() {
  const rows = decorated(), query = $('search').value.trim().toUpperCase();
  const selected = rows.filter(r => r.symbol.includes(query)).sort((a, b) => {
    const av = a[sortKey], bv = b[sortKey];
    if (av == null) return bv == null ? 0 : 1;
    if (bv == null) return -1;
    return (typeof av === 'string' ? av.localeCompare(bv) : av - bv) * sortDirection;
  });
  $('market-body').innerHTML = selected.length ? selected.map(r => `<tr data-coin="${escape(r.id)}"><td class="rank">${String(r.rank).padStart(2, '0')}</td><td><div class="coin-cell">${badge(r)}</div></td><td>${price(r.last)}</td><td><span class="change ${tone(r.change)}">${signed(r.change)}${r.change === null ? '' : '%'}</span></td><td>${compact(r.turnover)}</td><td title="${funding.has(r.id) ? '获取：' + time(funding.get(r.id).at) + ' UTC+7；下次结算：' + time(funding.get(r.id).settlement) : '等待获取'}" class="${tone(r.funding)}">${r.funding === null ? '—' : signed(r.funding * 100, 4) + '%'}</td><td title="${interests.has(r.id) ? '获取：' + time(interests.get(r.id).at) + ' UTC+7' : '等待获取'}">${compact(r.oi)}</td></tr>`).join('') : `<tr><td colspan="7" class="empty">${tickers.length ? '没有匹配的币种' : '行情尚未加载，正在重试…'}</td></tr>`;
  if (!rows.length) return;
  const up = rows.filter(r => r.change > 0).length, down = rows.filter(r => r.change < 0).length;
  const leader = rows.filter(r => r.change !== null).sort((a, b) => b.change - a.change)[0];
  const oiRows = rows.filter(r => r.oi !== null);
  $('market-stats').innerHTML = `<article class="stat"><span>样本成交额 · 24 小时</span><strong>${compact(rows.reduce((s, r) => s + (r.turnover || 0), 0))}</strong><small>前 ${rows.length} 合约 · USDT 估算</small></article><article class="stat"><span>上涨 / 下跌</span><strong><span class="up">${up}</span> <small>/</small> <span class="down">${down}</span></strong><small>前 ${rows.length} 合约 · 平盘 ${rows.filter(r => r.change === 0).length}</small></article><article class="stat"><span>涨幅领先</span><strong>${escape(leader?.symbol || '—')} <small class="${tone(leader?.change)}">${signed(leader?.change)}%</small></strong><small>按 24 小时价格变动排名</small></article><article class="stat"><span>持仓总额</span><strong>${oiRows.length ? compact(oiRows.reduce((s, r) => s + r.oi, 0)) : '—'}</strong><small>${oiRows.length} / ${rows.length} 合约 · USDT 估算</small></article>`;
}
function renderQuote() {
  const ticker = tickers.find(r => r.id === coin);
  $('coin-quote').innerHTML = ticker ? `<span class="${tone(ticker.change)}">${price(ticker.last)} USDT　${signed(ticker.change)}%</span>　<span class="muted">24h 变动 · ${time(priceAt)} UTC+7 更新</span>` : '等待该合约实时行情';
}
// Market page: top contracts by 24h turnover. Funding rates are one request per contract, so ranks past
// FUNDING_FAST refresh every 3 minutes instead of every minute to stay well inside OKX's public rate limit.
const MARKET_SIZE = 100, FUNDING_FAST = 30;
async function refreshPrices() {
  if (priceBusy) return;
  priceBusy = true;
  try {
    let data, source = 'okx';
    try {
      data = normalizeTickers(await api.get('market/tickers', { instType: 'SWAP' }, 10));
      if (!data.length) throw new Error('接口未返回有效 USDT 永续行情');
    } catch (error) {
      // OKX public REST unreachable from this network (both domains): the server's cached tickers, about a minute old.
      const rows = serverTickerRows(await fetchJSON(LIVE_TICKERS_URL, 8000).catch(() => null));
      if (!rows) throw error;
      data = normalizeTickers(rows); source = 'server';
      if (quoteSource !== 'server') diagNote(`OKX 公共行情连不上（${description(error)}），改用服务器行情`);
    }
    tickers = data; quoteSource = source;
    top = data.filter(r => r.turnover !== null).sort((a, b) => b.turnover - a.turnover).slice(0, MARKET_SIZE).map((r, i) => ({ ...r, rank: i + 1 }));
    priceAt = Date.now(); setError('prices', null);
    $('price-updated').textContent = `${time(priceAt)} UTC+7 更新${source === 'server' ? ' · 备用：服务器行情' : ''}`;
    renderMarket(); if (route === 'coin') renderQuote();
    if (source === 'server') { setError('oi', null); setError('funding', null);
      $('slow-updated').textContent = 'OKX 公共接口暂时连不上：行情来自服务器备用源（约每分钟），资金费率与持仓量暂停更新'; }
    else if (['market', 'coin'].includes(route)) refreshSlow();
  } catch (error) { quoteSource = null; setError('prices', `行情：${description(error)}`); if (!tickers.length) renderMarket(); }
  finally { priceBusy = false; updateStreamStatus(); }
}
async function refreshSlow() {
  if (slowBusy || !top.length || quoteSource === 'server') return;   // OKX unreachable: no funding / open interest to read
  slowBusy = true;
  const wanted = [...new Set([...(coin ? [coin] : []), ...top.map(r => r.id)])], slowRank = new Set(top.slice(FUNDING_FAST).map(r => r.id));
  try {
    const tasks = [];
    if (Date.now() - slowAt >= 60000) tasks.push((async () => {
      try {
        const rows = await api.get('public/open-interest', { instType: 'SWAP' });
        if (!rows.some(r => number(r.oiCcy) !== null)) throw new Error('接口持仓量为空');
        const at = Date.now();
        rows.forEach(r => { const qty = number(r.oiCcy); if (qty !== null) interests.set(r.instId, { qty, at }); });
        slowAt = at; setError('oi', null);
      } catch (error) { setError('oi', `持仓量：${description(error)}`); }
    })());
    const due = wanted.filter(id => Date.now() - (funding.get(id)?.at || 0) >= (slowRank.has(id) && id !== coin ? 180000 : 60000));
    if (due.length) tasks.push((async () => {
      const results = await Promise.allSettled(due.map(async id => {
        const rows = await api.get('public/funding-rate', { instId: id });
        const row = rows.find(r => r.instId === id), rate = number(row?.fundingRate);
        if (rate === null) throw new Error('接口资金费率为空');
        funding.set(id, { rate, at: Date.now(), settlement: number(row.fundingTime) });
        if (route === 'market') renderMarket();
      }));
      const failed = results.filter(r => r.status === 'rejected');
      setError('funding', failed.length ? `资金费率：${failed.length} 个合约获取失败` : null);
    })());
    await Promise.all(tasks);
    $('slow-updated').textContent = `持仓量与前 30 资金费率 60 秒刷新，其余资金费率 3 分钟 · ${slowAt ? '最近持仓数据 ' + time(slowAt) + ' UTC+7' : '持仓量等待获取'}`;
    if (route === 'market') renderMarket();
  } finally { slowBusy = false; }
}

// ---- coin page: candles, indicators, support / resistance ----
function renderAnalysis(bars) {
  const data = analyze(bars, null);
  $('levels').innerHTML = data.levels ? [20, 50].map(n => `<div class="level-row"><span>前 ${n} 根</span><span>支撑 ${price(data.levels['low' + n])}</span><span>阻力 ${price(data.levels['high' + n])}</span></div>`).join('') : '暂无区间数据';
}
function chartLevels() {
  // Live position entry / stop / target on the coin chart.
  const p = live?.positions.find(x => x.inst === coin);
  if (!p) return [];
  return [['开仓', p.avgPx, '#345db5'], ['止损', p.stop, '#c53045'], ['止盈', p.target, '#087c50']]
    .filter(([, v]) => Number.isFinite(v)).map(([title, value, color]) => ({ title, price: value, color }));
}
function updateChartOverlays() {
  if (!candleCharts) return;
  const levels = chartLevels();
  candleCharts.levels(levels);
  $('chart-levels').innerHTML = levels.map(l => `<span style="color:${l.color}">${l.title} ${price(l.price)}</span>`).join('');
}
function showCandles(bars, at) {
  renderAnalysis(bars);
  $('candle-updated').textContent = `${bars.length} 根 · ${time(at)} 更新${candleSource === 'server' ? ' · 备用：服务器 K 线' : ''}`;
  if (!chartAvailable()) { $('chart-error').hidden = false; $('chart-error').textContent = '图表库加载失败，请检查 CDN 连接后刷新页面。'; return; }
  if (route !== 'coin') return;
  try {
    if (!candleCharts) candleCharts = createCandleCharts();
    candleCharts.update(bars); updateChartOverlays(); $('chart-error').hidden = true;
  } catch { $('chart-error').hidden = false; $('chart-error').textContent = '图表暂时无法绘制，指标数据仍可查看。'; }
}
async function refreshCandles(force = false) {
  if (route !== 'coin') return;
  const id = coin, bar = timeframe, key = id + '/' + bar, version = routeVersion;
  const cached = candleCache.get(key);
  if (candleBusy.has(key) || (!force && cached && Date.now() - cached.at < 60000)) return;
  candleBusy.add(key); lastCandleRequest = key;
  try {
    const got = await candles('market/candles', { instId: id, bar, limit: 300 }, 8), bars = parseCandles(got.data);
    if (!bars.length) throw new Error('接口未返回有效 K 线');
    const at = Date.now(); candleCache.set(key, { bars, at });
    if (route !== 'coin' || coin !== id || timeframe !== bar) return;
    if (version !== routeVersion && lastCandleRequest !== key) return;
    if (got.source !== candleSource) diagNote(got.source === 'server' ? 'OKX K 线连不上，改用服务器 K 线' : 'K 线恢复 OKX 直连');
    candleSource = got.source; setError('candles', null); showCandles(bars, at); updateStreamStatus();
  } catch (error) { if (route === 'coin' && coin === id && timeframe === bar) setError('candles', `K 线：${description(error)}`); }
  finally { candleBusy.delete(key); }
}

// ---- live account (live-data branch) ----
async function fetchJSON(url, timeout = 12000) {
  const source = new URL(url); source.searchParams.set('t', Math.floor(Date.now() / 60000));   // cache-bust once a minute
  const response = await fetch(source, { cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(timeout) });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}
// Server API first (fresh every minute); live-data (hourly) when the API cannot be reached. Falling back is silent.
async function readLive() {
  try {
    const doc = splitLiveApi(await fetchJSON(LIVE_API_URL, 8000));
    if (doc) return { ...doc, source: 'api' };
    diagNote('服务器接口：数据格式不对，改用 live-data');
  } catch (e) { if (liveSource !== 'github') diagNote(`服务器接口连不上（${e.name || '错误'}），改用 live-data`); }
  const [snap, list, closed] = await Promise.allSettled([fetchJSON(LIVE_SNAPSHOT_URL), fetchJSON(LIVE_FILLS_URL), fetchJSON(LIVE_TRADES_URL)]);
  if (snap.status !== 'fulfilled' || !validLiveSnapshot(snap.value)) throw new Error('实盘快照读取失败');
  return { snapshot: snap.value, source: 'github', account: null,
    fills: list.status === 'fulfilled' && validLiveFills(list.value) ? list.value : null,
    // trades.json is newer than the other two files: missing (older sync) just leaves the panels on their empty state.
    trades: closed.status === 'fulfilled' && validLiveTrades(closed.value) ? closed.value : null };
}
async function refreshLive() {
  if (liveBusy) return; liveBusy = true;
  try {
    const next = await readLive();
    liveSource = next.source; liveAccount = next.account;
    // Never step back in time: an hourly live-data snapshot older than the API data already shown is ignored.
    if (!live || Date.parse(next.snapshot.updated_at) >= Date.parse(live.updated_at)) {
      const lastUpl = new Map(live ? liveView(live, marks).positions.map(p => [p.inst, p.upl]) : []);
      live = next.snapshot;
      if (next.fills) {
        const fresh = liveNewFills(fills, next.fills);
        fills = next.fills;
        fresh.map(a => tradeBurst(a, lastUpl)).filter(Boolean).forEach((b, i) => setTimeout(() => { mascot.burst(b); notify({ text: `实盘 · ${b.title}${b.amount ? ' ' + b.amount : ''}`, mood: b.mood }); }, i * 3400));
      }
      if (next.trades) trades = next.trades;
    }
    setError('fills', next.fills || fills ? null : '实盘成交记录读取失败，稍后自动重试');
    setError('live', null); $('live-error').hidden = true;
    syncStreams(); renderLive(); updateChartOverlays(); updateStreamStatus();
  } catch {
    setError('live', '实盘快照暂时读取失败，保留上次数据');
    $('live-error').hidden = false;
    $('live-error').textContent = live ? '实盘数据暂时读取失败，显示上次数据，稍后自动重试。' : '实盘数据暂时读取失败（可能是网络或 GitHub 暂时不可用），稍后自动重试。';
    renderLive();
  }
  finally { liveBusy = false; }
}
function renderLight() {
  const el = $('live-light'), l = liveLight(live);
  if (el.dataset.state !== l.state) el.dataset.state = l.state;
  setHTML(el, liveLightMarkup(live)); if (el.title !== l.title) el.title = l.title;
}
function renderLive() {
  const v = liveView(live, marks), fresh = liveFreshness(live), status = liveStatus(live);
  renderLight();
  renderPinned(v, fresh);
  updateCompanion(v);
  renderStatusPill();
  if (route !== 'home') return;
  if (!live) { setText($('live-fresh'), fresh.text); return; }
  rollNumber($('live-equity'), v.equity, .01);
  setHTML($('live-day'), `当日盈亏 <span class="${tone(v.day)}">${Number.isFinite(v.day) ? `${signed(v.day)} USDT（${signed(v.dayPct)}%）` : '—'}</span>` +
    (Number.isFinite(v.total) ? ` · 累计 <span class="${tone(v.total)}">${signed(v.total)} USDT（${signed(v.totalPct)}%）</span>` : ''));
  setHTML($('live-meta'), `可用 <b>${num(v.available)}</b> USDT · 持仓保证金 ${num(v.margin)} USDT · 浮盈 <span class="${tone(v.upl)}">${signed(v.upl)}</span> USDT${v.live ? '（实时标记价）' : '（快照）'}`);
  const st = $('live-status'), cls = `pill okx-pill ${status.cls}`;
  setText(st, status.label); if (st.className !== cls) st.className = cls;
  setText($('live-fresh'), fresh.text); $('live-fresh').classList.toggle('stale', fresh.stale);
  setHTML($('live-curve'), liveCurveSVG(live));
  setHTML($('live-daily'), okxDailyMarkup(okxDailyReturns(live, v.equity), 10));
  renderPositions(v);
  setText($('live-pos-count'), String(live.positions.length));
  setHTML($('live-fills'), fills ? liveFillsMarkup(fills.fills, 15) : '<li class="empty">成交记录读取中…</li>');
  setHTML($('live-events'), fills ? liveFillsMarkup(fills.events, 30, '暂无运行记录') : '');
  setHTML($('live-stats'), tradeStatsMarkup(trades || { trades: [] }, live));
  setHTML($('live-trades'), closedTradesMarkup(trades || { trades: [] }));
  setText($('live-trade-count'), String(trades?.stats?.overall?.trades ?? trades?.trades?.length ?? 0));
}

// ---- K-line popup for a position / closed trade: OKX public candles (browser side), levels as a fallback ----
let tradeChart = null, tradeChartToken = 0;
async function openTradeChart(subject) {
  const dlg = $('trade-chart'); if (!subject) return;
  const token = ++tradeChartToken;
  tradeChart?.destroy(); tradeChart = null;
  const box = $('trade-chart-box'); box.replaceChildren(); box.classList.remove('fallback');
  $('trade-chart-title').textContent = subject.title;
  $('trade-chart-meta').textContent = [subject.opened ? `入场 ${time(subject.opened)}` : '', subject.closed ? `出场 ${time(subject.closed)}` : '', 'UTC+7'].filter(Boolean).join(' · ');
  $('trade-chart-levels').innerHTML = subject.levels.map(l => `<span style="color:${levelColor(l.role)}">${escape(l.title)} ${livePx(l.price)}</span>`).join('');
  $('trade-chart-note').textContent = '正在读取 OKX 公共 K 线…';
  if (!dlg.open) { if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', ''); }
  const fallback = why => { if (token !== tradeChartToken) return; box.classList.add('fallback'); box.innerHTML = levelsLadderSVG(subject); $('trade-chart-note').textContent = `${why}，先显示入场 / 止损 / 止盈价位参考。`; };
  if (!chartAvailable()) return fallback('图表库没有加载');
  const req = candleRequest(subject);
  try {
    const got = await candles(req.path, req.params, 9), bars = parseCandles(got.data);
    if (token !== tradeChartToken || !dlg.open) return;
    if (!bars.length) return fallback('没有取到 K 线');
    tradeChart = createTradeChart(box);
    tradeChart.draw(bars, subject.levels.map(l => ({ ...l, color: levelColor(l.role) })), tradeMarkers(subject, bars));
    $('trade-chart-note').textContent = `${req.bar.replace('utc', '')} K 线 · ${bars.length} 根 · ${got.source === 'server' ? '服务器备用（OKX 公共行情）' : 'OKX 公共行情'}（含未收盘 K 线）`;
  } catch (error) { fallback(`K 线读取失败（${description(error)}）`); }
}
function closeTradeChart() { tradeChartToken++; tradeChart?.destroy(); tradeChart = null; const dlg = $('trade-chart'); if (dlg.open) { if (dlg.close) dlg.close(); else dlg.removeAttribute('open'); } }
$('trade-chart-close').addEventListener('click', closeTradeChart);
$('trade-chart').addEventListener('close', () => { tradeChartToken++; tradeChart?.destroy(); tradeChart = null; });
$('trade-chart').addEventListener('click', e => { if (e.target.id === 'trade-chart') closeTradeChart(); });
$('live-positions').addEventListener('click', e => {
  const b = e.target.closest('[data-chart-inst]'); if (!b) return;
  e.preventDefault(); e.stopPropagation();   // inside <summary>: do not toggle the row
  const p = liveView(live, marks)?.positions.find(x => x.inst === b.dataset.chartInst);
  openTradeChart(chartSubject(p, 'position'));
});
const tradeAt = el => trades?.trades?.[Number(el.dataset.trade)];
$('live-trades').addEventListener('click', e => { const r = e.target.closest('[data-trade]'); if (r) openTradeChart(chartSubject(tradeAt(r), 'trade')); });
$('live-trades').addEventListener('keydown', e => { const r = e.target.closest('[data-trade]'); if (r && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openTradeChart(chartSubject(tradeAt(r), 'trade')); } });
// Marks tick several times a second: patch only the changed parts of each row so open rows stay open.
function renderPositions(v) {
  const box = $('live-positions'), tpl = document.createElement('template');
  tpl.innerHTML = livePositionsMarkup(live, v);
  const next = [...tpl.content.querySelectorAll('details.okx-pos')], cur = [...box.querySelectorAll('details.okx-pos')];
  if (next.length && next.length === cur.length && next.every((n, i) => n.dataset.inst === cur[i].dataset.inst && n.className === cur[i].className)) {
    next.forEach((n, i) => { for (const sel of ['.okx-side', '.okx-pos-tag', '.okx-pos-pnl', '.okx-pos-body']) {
      const was = cur[i].querySelector(sel), now = n.querySelector(sel); if (was && now && was.outerHTML !== now.outerHTML) was.replaceWith(now);
    } });
    return;
  }
  const open = new Map(cur.map(el => [el.dataset.inst, el.open]));
  box.replaceChildren(tpl.content);
  box.querySelectorAll('details.okx-pos').forEach(el => { if (open.has(el.dataset.inst)) el.open = open.get(el.dataset.inst); });
}
function renderPinned(v, fresh) {
  if (!v) return;
  setText($('pinned-source'), push.live && live?.positions.length ? 'OKX 实盘 · 服务器实时推送' : liveSource === 'api' ? 'OKX 实盘 · 服务器约每分钟刷新' : 'OKX 实盘 · 约每小时同步');
  rollNumber($('pinned-equity'), v.equity, .01);
  const pct = $('pinned-pct'); pct.textContent = Number.isFinite(v.dayPct) ? `${signed(v.dayPct)}%` : '—'; pct.className = `pinned-pct ${tone(v.dayPct)}`;
  $('pinned-daily').textContent = `今日 ${Number.isFinite(v.day) ? `${signed(v.day)}（${signed(v.dayPct)}%）` : '—'}${fresh.stale ? ' · 离线' : ''}`;
  $('pinned-daily').className = tone(v.day);
  $('pinned-time').textContent = `${fresh.stale ? '延迟 · ' : ''}${time(live.updated_at)} UTC+7`; $('pinned-time').className = fresh.stale ? 'stale' : '';
}
function renderStatusPill() {
  const pill = $('status-pill'), label = $('status-pill-label'), body = $('status-pill-body');
  const list = [...errors.values()], fresh = liveFreshness(live), failed = list.length > 0;
  label.textContent = failed ? `● ${list.length} 项读取失败` : live ? (fresh.stale ? '● 实盘数据离线' : `● 实盘 · ${fresh.minutes < 1 ? '刚刚' : fresh.minutes < 90 ? fresh.minutes + ' 分钟前' : Math.round(fresh.minutes / 60) + ' 小时前'}更新`) : '● 等待更新';
  pill.classList.toggle('stale', fresh.stale && !failed && Boolean(live));
  pill.classList.toggle('error', failed);
  const lines = [live ? fresh.text : '正在读取实盘快照…', sourceText(liveSource, liveAccount, push.live && Boolean(live?.positions.length))];
  if (quoteSource === 'server') lines.push('OKX 公共行情暂时连不上，价格与浮盈改用服务器数据（约每分钟更新）。');
  if (failed) lines.push('读取失败：' + list.join('；') + '。已有数据保留，自动重试。');
  setHTML(body, lines.map(s => `<p>${escape(s)}</p>`).join(''));
}

// ---- companion ----
const pnlMemo = { upl: null, at: 0 };
function updateCompanion(v = liveView(live, marks)) {
  const positions = v ? v.positions.map(p => ({ inst: p.inst, side: p.side, upl: p.upl, ours: p.ours })) : [];
  mascot.update(null, null, { route, coin, paused: Boolean(live?.paused), simState: liveMascotState(live, fills), simFeed: liveFeed(fills),
    live: v ? { equity: v.equity, day: v.day, dayPct: v.dayPct, positions: positions.length, available: v.available } : null,
    report: positionReport(positions) });
  if (!v) return;
  const work = workState({ positions, equity: v.equity, paused: Boolean(live.paused), halted: Boolean(live.halted), offline: liveFreshness(live).stale });
  mascot.setWork(work, pnlMood(work.upl, v.equity));
  if (mascot.floating) mascot.setPipInfo({ equity: `${num(v.equity)} U`, line: `今日 ${Number.isFinite(v.day) ? signed(v.day) : '—'} · ${work.label}`, tone: tone(v.day) });
  const c = pnlComment(pnlMemo, { upl: work.upl, equity: v.equity, positions }); if (c) mascot.say(c.text, c.mood);
}

// ---- streams: OKX public mark prices (live positions + coin page) and candles ----
function updateStreamStatus() {
  const quotesLive = quoteStream?.state === '实时' && Date.now() - quoteStream.lastData < 45000, recent = priceAt && Date.now() - priceAt < 24000;
  // Fallbacks count as connected: the server's tickers, or (home page) the server's account read with OKX marks and PnL.
  const server = (recent && quoteSource === 'server') || (liveSource === 'api' && live && Date.now() - Date.parse(live.updated_at) < API_FRESH);
  const badge = connectionBadge({ push: push.live && Boolean(live?.positions.length), ws: quotesLive, rest: recent && quoteSource === 'okx', server });
  setText($('connection-text'), badge.text);
  $('connection-dot').className = 'dot ' + (badge.ok ? 'live' : 'error');
  document.documentElement.classList.toggle('has-live-state', badge.ok);
}
let liveRenderPending = 0;
function scheduleLiveRender() {
  // Marks arrive many times a second; redraw at most once a second.
  if (liveRenderPending) return;
  liveRenderPending = setTimeout(() => requestAnimationFrame(() => { liveRenderPending = 0; renderLive(); if (route === 'coin') renderQuote(); }), 1000);
}
function syncStreams() {
  if (document.hidden) return;
  push.start();
  const symbols = [...new Set([...(route === 'coin' ? [coin] : []), ...(live?.positions || []).map(p => p.inst)])].filter(Boolean).sort();
  const key = symbols.join(',');
  if (key !== quoteStreamKey) {
    quoteStream?.stop(); quoteStream = null; quoteStreamKey = key;
    if (symbols.length) {
      quoteStream = new PublicStream({ url: 'wss://ws.okx.com:8443/ws/v5/public', fallbackUrls: ['wss://wsaws.okx.com:8443/ws/v5/public'], args: symbols.map(instId => ({ channel: 'mark-price', instId })), maxFailures: config.wsMaxFailures,
        onState: (state, reason) => { diagNote(`行情 WS：${state}${reason ? ' · ' + reason : ''}`); updateStreamStatus(); }, onData: body => {
          const id = body.arg?.instId; if (!symbols.includes(id)) return false;
          const row = body.data[0], value = number(row.markPx), at = number(row.ts);
          if (!(value > 0) || !at || Date.now() - at > 45000 || at > Date.now() + 60000) return false;
          marks.set(id, { price: value, at: Date.now() });
          scheduleLiveRender();
          return true;
        } });
      quoteStream.start();
    }
  }
  const candleKey = route === 'coin' ? coin + '/' + timeframe : '';
  if (candleKey !== candleStreamKey) {
    candleStream?.stop(); candleStream = null; candleStreamKey = candleKey;
    if (candleKey) { const id = coin, bar = timeframe;
      candleStream = new PublicStream({ url: 'wss://ws.okx.com:8443/ws/v5/business', fallbackUrls: ['wss://wsaws.okx.com:8443/ws/v5/business'], args: [{ channel: 'candle' + bar, instId: id }], maxFailures: config.wsMaxFailures,
        onState: () => updateStreamStatus(), onData: body => {
          if (body.arg?.instId !== id || body.arg?.channel !== 'candle' + bar) return false;
          const next = parseCandles(body.data.filter(Array.isArray)).at(-1); if (!next || Date.now() / 1000 - next.time > (bar === '1H' ? 7200 : 28800)) return false;
          const cached = candleCache.get(candleKey); if (!cached) return false;   // REST seeds history first.
          const bars = mergeCandle(cached.bars, next), at = Date.now(); candleCache.set(candleKey, { bars, at });
          if (coin === id && timeframe === bar && route === 'coin') { renderAnalysis(bars); if (chartAvailable()) {
            if (!candleCharts) candleCharts = createCandleCharts(); candleCharts.update(bars); updateChartOverlays();
          } $('candle-updated').textContent = `${bars.length} 根 · ${time(at)} 实时更新`; }
          return true;
        } });
      candleStream.start();
    }
  }
}

// ---- connection diagnostics ----
const ago = ms => ms ? `${Math.round((Date.now() - ms) / 1000)} 秒前` : '从未';
function diagText() {
  const ws = s => s ? `${s.state} · ${s.activeUrl || s.url} · 最近数据 ${ago(s.lastData)} · 连续失败 ${s.failures}` : '未启动';
  const marksText = [...marks].map(([k, v]) => `${k.split('-')[0]} ${v.price}（${ago(v.at)}）`).join('，') || '无';
  return [`版本：${BUILD}`, `时间：${new Date().toISOString()}`, `页面可见：${!document.hidden} · 路由：${route}`, `浏览器：${navigator.userAgent}`,
    `行情 REST：最近成功 ${ago(priceAt)} · 合约数 ${tickers.length} · 来源 ${quoteSource === 'server' ? '服务器备用' : api.base || '连不上'}`,
    `实盘账户来源：${liveSource === 'api' ? '服务器只读接口' : liveSource === 'github' ? 'GitHub live-data' : '未读取'}`, `当前报错：${[...errors.values()].join(' / ') || '无'}`,
    `标记价 WS（${quoteStreamKey || '无订阅'}）：${ws(quoteStream)}`, `服务器推送：${push.state} · 最近数据 ${ago(push.lastData)} · 连续失败 ${push.failures}`, `标记价：${marksText}`,
    `K 线来源：${candleSource === 'server' ? '服务器备用' : 'OKX'}`,
    `实盘快照：${live ? live.updated_at + ' · 持仓 ' + live.positions.length : '未读取'}`, '最近事件：', ...(diagLog.length ? diagLog : ['（无）'])].join('\n');
}
function openDiag() { $('diag-body').textContent = diagText(); $('diag').hidden = false; $('diag-close').focus(); }
$('connection').addEventListener('click', openDiag);
$('diag-close').addEventListener('click', () => { $('diag').hidden = true; $('connection').focus(); });
$('diag').addEventListener('click', e => { if (e.target.id === 'diag') $('diag').hidden = true; });
$('diag-copy').addEventListener('click', async () => { try { await navigator.clipboard.writeText(diagText() + '\n' + $('diag-test-out').textContent); $('diag-copy').textContent = '已复制'; } catch { $('diag-copy').textContent = '复制失败，请长按选取'; } });
$('diag-reconnect').addEventListener('click', () => {
  quoteStream?.stop(); candleStream?.stop(); push.stop(); quoteStream = candleStream = null; quoteStreamKey = candleStreamKey = '';
  diagNote('用户主动重新连接行情'); syncStreams(); refreshPrices(); refreshLive();
  $('diag-body').textContent = diagText();
});
function probeSocket(url, instId) {
  return new Promise(resolve => {
    const t0 = Date.now(); let ws, done = false; const end = msg => { if (done) return; done = true; try { ws.close(); } catch {} resolve(`${msg}（${Date.now() - t0} ms）`); };
    try { ws = new WebSocket(url); } catch (e) { return resolve(`无法创建：${e.message}`); }
    const timer = setTimeout(() => end('8 秒内没有数据'), 8000);
    ws.onopen = () => ws.send(JSON.stringify({ op: 'subscribe', args: [{ channel: 'mark-price', instId }] }));
    ws.onmessage = e => { const b = (() => { try { return JSON.parse(e.data); } catch { return {}; } })(); if (b.event === 'error') { clearTimeout(timer); end(`返回错误 ${b.code} ${b.msg}`); } else if (b.data?.[0]?.markPx) { clearTimeout(timer); end(`成功，标记价 ${b.data[0].markPx}`); } };
    ws.onerror = () => { clearTimeout(timer); end('连接出错（onerror）'); };
    ws.onclose = e => { clearTimeout(timer); end(`连接关闭 code ${e.code}${e.reason ? ' ' + e.reason : ''}`); };
  });
}
async function probeRest(url) {
  const t0 = Date.now();
  try { const r = await fetch(url, { cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(8000) }); const text = await r.text(); return `HTTP ${r.status} · ${text.slice(0, 80)}（${Date.now() - t0} ms）`; }
  catch (e) { return `失败：${e.name} ${e.message}（${Date.now() - t0} ms）`; }
}
$('diag-test').addEventListener('click', async () => {
  const out = $('diag-test-out'); out.textContent = '测试中…（最多约 10 秒）';
  const [rest, aws, server, data, socket] = await Promise.all([probeRest(OKX_REST_BASES[0] + 'public/time'), probeRest(OKX_REST_BASES[1] + 'public/time'),
    probeRest(LIVE_API_URL.replace('live.json', 'healthz')), probeRest(LIVE_SNAPSHOT_URL + '?t=' + Date.now()), probeSocket('wss://ws.okx.com:8443/ws/v5/public', 'BTC-USDT-SWAP')]);
  out.textContent = `REST www.okx.com：${rest}\nREST aws.okx.com：${aws}\n服务器接口：${server}\n实盘快照 raw.githubusercontent.com：${data.replace(/·.*（/, '· （')}\n行情 WS ws.okx.com：${socket}`;
  $('diag-body').textContent = diagText();
});

// ---- 策略进化 (tools/evolve → paper-data/evolve/dashboard.json; engine data only, no simulated account) ----
// dashboard.json is ~1 MB and changes every 4 h: fetched only on the evolve page, at most every 5 minutes.
let evolveData = null, evolveBusy = false, evolveSel = null, evolveAt = 0;
async function refreshEvolve(force = false) {
  if (evolveBusy || route !== 'evolve' || (!force && evolveData && Date.now() - evolveAt < 300000)) return; evolveBusy = true;
  try {
    const r = await fetch(`${EVOLVE_URL}?t=${Math.floor(Date.now() / 300000)}`, { cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(20000) });
    if (!r.ok) throw new Error('读取失败'); const d = await r.json(); if (!validEvolve(d)) throw new Error('格式不对');
    evolveData = d; evolveAt = Date.now(); $('evolve-error').hidden = true; setError('evolve', null); renderEvolve();
  } catch {
    const msg = '进化数据暂时读取失败，稍后自动重试。'; setError('evolve', msg);
    if (!evolveData) { const intro = $('evolve-intro'); if (intro) intro.innerHTML = `<p class="alert" role="alert">${msg}</p>`; if (route === 'evolve') { $('evolve-error').hidden = false; $('evolve-error').textContent = msg; } }
  } finally { evolveBusy = false; }
}
// Per-node genes and curves (nodes.json), fetched only when a node without them is opened; once per generation.
const evolveDetails = { data: null, tried: null, busy: false, failed: false };
async function loadEvolveDetails() {
  const gen = evolveData?.generation; if (evolveDetails.busy || evolveDetails.tried === gen) return;
  evolveDetails.busy = true; evolveDetails.tried = gen;
  try {
    const r = await fetch(`${EVOLVE_NODES_URL}?t=${Math.floor(Date.now() / 300000)}`, { cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(20000) });
    const x = r.ok ? await r.json() : null; if (!validNodeDetails(x)) throw new Error('bad'); evolveDetails.data = x; evolveDetails.failed = false;
  } catch { evolveDetails.failed = true; setTimeout(() => { if (evolveDetails.tried === gen) evolveDetails.tried = null; renderEvolve(); }, 60000); }
  finally { evolveDetails.busy = false; renderEvolve(); }
}
function renderEvolve() {
  const d = evolveData;
  $('home-evolve').hidden = false; setHTML($('home-evolve'), homeCardMarkup(d));
  if (route !== 'evolve' || !d) return;
  if (!evolveSel || !d.nodes.some(n => n.id === evolveSel)) evolveSel = d.champion?.id || d.nodes[0]?.id || null;
  { const u = updateSchedule(d.updated_at); setText($('evolve-updated'), `第 ${d.generation} 代 · ${u.line}`); $('evolve-updated').classList.toggle('stale', u.stale); setHTML($('evolve-intro'), evolveIntroMarkup(d)); }
  setHTML($('evolve-loop'), loopSVG(d)); setHTML($('evolve-stats'), statsMarkup(d)); setHTML($('evolve-fitness'), fitnessSVG(d.history));
  setHTML($('evolve-graph'), graphSVG(d, evolveSel)); geneFx.refresh(); setHTML($('evolve-detail'), detailMarkup(d, evolveSel, evolveDetails.data, evolveDetails.failed));
  if (needsDetails(d, evolveSel, evolveDetails.data)) loadEvolveDetails();
  // On narrow screens the gene pool scrolls sideways: start at the newest generation unless the viewer has scrolled.
  { const box = $('evolve-graph'); if (!box.dataset.userScrolled && box.scrollWidth > box.clientWidth) { box.dataset.auto = '1'; box.scrollLeft = box.scrollWidth; } }
  setHTML($('evolve-team'), teamMarkup(d)); setText($('evolve-caption'), dataCaption(d));
  // Replay: the whole history on first view, only the new generations when fresh data arrives.
  const gens = replayGenerations(d);
  if (evoReplay.timer) evoApply(gens[evoReplay.i - 1] ?? gens[0]);
  else if (evoReplay.seen === null) { evoReplay.seen = d.generation; if (!evoStill()) evoPlay(gens[0]); else evoShowAll(); }
  else if (d.generation > evoReplay.seen) { const from = evoReplay.seen + 1; evoReplay.seen = d.generation; if (!evoStill()) evoPlay(from); }
  else if (!evoReplay.timer) evoShowAll();
}
// ---- gene pool replay (generation by generation) ----
const evoReplay = { timer: 0, gens: [], i: 0, seen: null };
const evoStill = () => matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.classList.contains('reduced-motion');
const geneFx = new GeneFx($('evolve-graph'), { still: evoStill, active: () => route === 'evolve' });
function evoApply(g) {
  const svg = $('evolve-graph').querySelector('svg'); if (!svg || g === undefined) return;
  svg.classList.add('replay');
  svg.querySelectorAll('[data-born]').forEach(el => { const st = replayState(el.dataset, g); el.classList.toggle('future', st.future); el.classList.toggle('gone', st.gone); el.classList.toggle('newborn', st.newborn); });
  const label = svg.querySelector('.evo-gen-label'); if (label) label.textContent = `第 ${g} 代`;
}
function evoShowAll() {
  const svg = $('evolve-graph').querySelector('svg'); if (svg) { svg.classList.remove('replay'); svg.querySelectorAll('.future,.gone,.newborn').forEach(el => el.classList.remove('future', 'gone', 'newborn')); }
}
function evoStop(showAll = true) { clearTimeout(evoReplay.timer); evoReplay.timer = 0; if (showAll) evoShowAll(); }
function evoPlay(fromGen) {
  if (!evolveData) return;
  clearTimeout(evoReplay.timer);
  evoReplay.gens = replayGenerations(evolveData);
  evoReplay.i = Math.max(0, evoReplay.gens.findIndex(g => g >= fromGen));
  const delays = replayDelays(evolveData, evoReplay.gens, fromGen > evoReplay.gens[0] ? 5000 : 12000);   // full history ≈ 12 s, new generations ≈ 5 s
  const tick = () => {
    if (route !== 'evolve' || document.hidden || evoReplay.i >= evoReplay.gens.length) { evoStop(); return; }
    const k = evoReplay.i; evoApply(evoReplay.gens[k]); evoReplay.i++; evoReplay.timer = setTimeout(tick, delays[k]);
  };
  evoReplay.timer = 1; tick();
}
function selectEvolveNode(id, focus = false) {
  if (!evolveData || !id) return; evolveSel = id; renderEvolve();
  if (focus) $('evolve-detail').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
$('evolve-graph').addEventListener('scroll', e => { const box = e.currentTarget; if (box.dataset.auto) { delete box.dataset.auto; return; } box.dataset.userScrolled = '1'; }, { passive: true });
$('evolve-page').addEventListener('click', e => { const n = e.target.closest('[data-node]'); if (n && evoReplay.timer) evoStop(); if (n) selectEvolveNode(n.dataset.node, n.closest('#evolve-detail') === null && innerWidth < 900); });
$('evolve-page').addEventListener('keydown', e => { const n = e.target.closest('[data-node]'); if (n && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); selectEvolveNode(n.dataset.node, true); } });

// ---- routing ----
function navigate() {
  routeVersion++;
  if (['#/paper', '#/sim'].includes(location.hash)) { location.replace('#/'); return; }   // retired pages (simulated account)
  const hash = location.hash || '#/';
  const match = /^#\/coin\/([A-Z0-9]+-USDT-SWAP)$/.exec(hash);
  route = match ? 'coin' : hash === '#/market' ? 'market' : hash === '#/evolve' ? 'evolve' : 'home';
  if (match) coin = match[1];
  candleCharts?.destroy(); candleCharts = null; $('chart-levels').replaceChildren();
  for (const page of ['home', 'market', 'coin', 'evolve']) $(`${page}-page`).hidden = route !== page;
  document.querySelectorAll('[data-nav]').forEach(a => { const active = a.dataset.nav === (route === 'coin' ? 'market' : route); a.classList.toggle('active', active); if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  document.querySelectorAll('[data-timeframe]').forEach(b => { const active = b.dataset.timeframe === timeframe; b.classList.toggle('active', active); b.setAttribute('aria-pressed', String(active)); });
  setError('candles', null);
  document.title = route === 'coin' ? `${coin.split('-')[0]} · MODI 实盘看板` : route === 'market' ? '市场行情 · MODI 实盘看板' : route === 'evolve' ? '策略进化 · MODI 交易团队' : 'MODI · AI 交易团队 · OKX 实盘看板';
  document.documentElement.dataset.route = route;
  if (route === 'coin') {
    $('coin-title').textContent = coin.replace('-USDT-SWAP', '') + ' / USDT 永续'; renderQuote();
    $('levels').innerHTML = '<p class="muted">等待 K 线数据</p>'; $('candle-updated').textContent = '加载中'; $('chart-error').hidden = true;
    const cached = candleCache.get(coin + '/' + timeframe); if (cached) showCandles(cached.bars, cached.at);
    refreshCandles(); refreshSlow();
  } else if (route === 'market') { renderMarket(); refreshSlow(); }
  if (route === 'evolve') { refreshEvolve(); queueMicrotask(() => geneFx.kick()); }   // resume the gene-pool motion
  renderEvolve(); renderLive(); syncStreams();
}
$('search').addEventListener('input', renderMarket);
$('market-table').addEventListener('click', event => {
  const button = event.target.closest('[data-sort]');
  if (button) {
    const key = button.dataset.sort;
    sortDirection = sortKey === key ? -sortDirection : ['symbol', 'rank'].includes(key) ? 1 : -1; sortKey = key;
    document.querySelectorAll('[data-sort]').forEach(b => {
      const th = b.closest('th'); th.setAttribute('aria-sort', b.dataset.sort === key ? sortDirection === 1 ? 'ascending' : 'descending' : 'none');
      b.innerHTML = b.innerHTML.replace(/ [↑↓]$/, '') + (b.dataset.sort === key ? sortDirection === 1 ? ' ↑' : ' ↓' : '');
    }); renderMarket(); return;
  }
  const row = event.target.closest('[data-coin]'); if (row && !event.target.closest('a')) location.hash = '#/coin/' + row.dataset.coin;
});
document.querySelectorAll('[data-timeframe]').forEach(button => button.addEventListener('click', () => {
  if (timeframe === button.dataset.timeframe) return;
  timeframe = button.dataset.timeframe; navigate();
}));
$('reset-chart').addEventListener('click', () => candleCharts?.reset());
window.addEventListener('hashchange', navigate);
window.addEventListener('load', () => { if (route === 'coin') { const cached = candleCache.get(coin + '/' + timeframe); if (cached) showCandles(cached.bars, cached.at); } });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { quoteStream?.stop(); candleStream?.stop(); push.stop(); quoteStream = candleStream = null; quoteStreamKey = candleStreamKey = ''; }
  else { syncStreams(); refreshPrices(); refreshLive(); if (route === 'coin') refreshCandles(true); }
});

(function initTheme() {
  const btn = $('theme-toggle'); if (!btn) return;
  const effective = () => document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const paint = () => { const now = effective(); btn.textContent = now === 'dark' ? '亮' : '暗'; btn.setAttribute('aria-pressed', String(now === 'dark')); btn.title = now === 'dark' ? '切换到浅色' : '切换到深色'; };
  paint();
  btn.addEventListener('click', () => { const next = effective() === 'dark' ? 'light' : 'dark'; document.documentElement.setAttribute('data-theme', next); try { localStorage.setItem('modi-theme', next); } catch {} paint(); });
})();
navigate();
refreshPrices(); refreshLive();
setInterval(() => { if (!document.hidden) { refreshPrices(); renderLive(); updateStreamStatus(); } }, 8000);
setInterval(() => { if (!document.hidden) { if (route === 'coin') refreshCandles(); refreshLive(); refreshSlow(); refreshEvolve();
  for (const stream of [quoteStream, candleStream]) if (stream?.state === '实时' && Date.now() - stream.lastData > 45000) stream.socket?.close(); } }, 60000);
const updateClock = () => { $('utc-clock').textContent = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Bangkok', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(Date.now()) + ' UTC+7'; $('day-number').textContent = 'DAY ' + dayNumber(config.startDate); };
updateClock(); setInterval(updateClock, 1000);

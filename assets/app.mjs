import { HomePositions, equitySVG, rangePoints } from './home.mjs';
import { initTradeControls, renderTradeControls } from './control.mjs';
import { runtimeConfig, refreshRuntimeHealth } from './runtime.mjs';
import { coinIcon } from './coins.mjs';
import { Mascot, fillLine, eventLine, reportLine } from './mascot.mjs';
import { riskProgress } from './history.mjs';
import { OKX_URL, OKX_STALE, OKX_COLOR, validOkx, okxTotals, okxStatus, okxCurveSVG, okxPositionsMarkup, okxActionsMarkup, okxMascotState, okxChanges, okxNewActions, okxLive, okxFeed, okxStart, okxDailyReturns, okxDailyMarkup, okxOutlier, okxOutlierNote, mirrorCompareMarkup, OKX_DEMO_PUBLIC_WS, OKX_DEMO_FALLBACK_WS, demoQuoteState, accountDataNote } from './okx.mjs?v=20261114-sidecap';
import { baseQuantity, positionAmounts, orderAmounts, quoteFor, manualOrders, pendingOrders, chartLevels } from './portfolio.mjs';
import { NewRecords, recordKey, roleState, isStale, validEvents, eventText } from './state.mjs';
import { PublicStream, mergeCandle, tradeMarkers } from './realtime.mjs?v=20261114-sidecap';
import { configureMotion, rollNumber, toast, typeLine } from './effects.mjs';
import { workState, pnlMood, tradeBurst, positionReport, pnlComment } from './robot-work.mjs';
import { EVOLVE_URL, EVOLVE_NODES_URL, validNodeDetails, needsDetails, validEvolve, loopSVG, statsMarkup, fitnessSVG, graphSVG, detailMarkup, paperMarkup, homeCardMarkup, dataCaption, replayGenerations, replayState, replayDelays, updateSchedule, evolveIntroMarkup } from './evolve.mjs?v=20261114-sidecap';
import { GeneFx } from './evolve-fx.mjs';
import { mainAccounts, liveBalance, validAccount, summarizeAccounts, aggregateCurve, dayNumber } from './account.mjs';
import { ROLES, validTeam, teamTime, timelineMarkup } from './team.mjs';
import config from '../config.js';
import { validSnapshot } from './dashboard.mjs';
import { validScan, scanRows, signalText, meetingSymbol, chartUniverse, strategyEntries, strategyName, reasonText } from './scan.mjs';
import { PublicAPI, normalizeTickers, number } from './api.mjs';
import { parseCandles, analyze } from './indicators.mjs';
import { chartAvailable, createCandleCharts } from './charts.mjs';

let decisionHistory=null,decisionHistoryStatus='loading',decisionHistoryBusy=false;
const $ = id => document.getElementById(id);
configureMotion();
const mascot = new Mascot($('mascot'), $('mascot-drawer'));
// Screen readers hear the toast; sighted users get the companion's speech bubble.
const notify = (line, announce = line.text) => { toast(announce); mascot.say(line.text, line.mood); };
const teamRecords=new NewRecords(),fillRecords=new NewRecords(),eventRecords=new NewRecords(),liveQuotes=new Map();
let quoteStream=null,candleStream=null,quoteStreamKey='',candleStreamKey='',streamReason='';
// Connection diagnostics (tap the status in the header): recent script errors and stream state changes.
const BUILD='2026-11-04 · 模拟行情 443 / 8443 双通道',diagLog=[];
// Streams report their state on every message; log only changes so real events stay visible.
const diagNote=text=>{if(diagLog.length && diagLog.at(-1).endsWith(' '+text))return;diagLog.push(`${new Date().toISOString().slice(11,19)}Z ${text}`);diagLog.splice(0,diagLog.length-12);};
addEventListener('error',e=>diagNote(`脚本错误：${e.message || e.type}${e.filename?' @ '+e.filename.split('/').pop()+':'+e.lineno:''}`));
addEventListener('unhandledrejection',e=>diagNote(`未处理的异常：${e.reason?.message || e.reason}`));
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const num = (value, digits = 2) => Number.isFinite(value) ? value.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
const price = value => num(value, value >= 100 ? 2 : value >= 1 ? 4 : value >= .01 ? 6 : 8);
const compact = value => !Number.isFinite(value) ? '—' : Math.abs(value) >= 1e8 ? num(value / 1e8) + ' 亿' : Math.abs(value) >= 1e4 ? num(value / 1e4) + ' 万' : num(value);
const signed = (value, digits = 2) => Number.isFinite(value) ? (value > 0 ? '+' : '') + num(value, digits) : '—';
const tone = value => Number.isFinite(value) ? value >= 0 ? 'up' : 'down' : '';
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const time = value => { const ms = typeof value === 'number' ? value : Date.parse(value); return Number.isFinite(ms) ? clock.format(ms) : '—'; };
const api = new PublicAPI(), errors = new Map(), funding = new Map(), interests = new Map(), candleCache = new Map();
let tickers = [], top = [], priceAt = 0, slowAt = 0, priceBusy = false, slowBusy = false, snapshotBusy = false;
const configuredSymbol = /^[A-Z0-9]+-USDT-SWAP$/.test(config.symbol) ? config.symbol : 'ZEC-USDT-SWAP';
let focusSymbol = configuredSymbol, homeCoin = focusSymbol, manualHomeCoin = null, previousActive = new Set();
let route = 'home', coin = focusSymbol, timeframe = '1H', routeVersion = 0, sortKey = 'turnover', sortDirection = -1;
let candleCharts = null, snapshot = null, equityRange = 'all';
const homePositions = new HomePositions($('live-positions'), $('home-price-slot'), position => {
  candleCharts?.destroy(); candleCharts=null;
  if(position) { manualHomeCoin=position.symbol; syncHomeCoin(); }
  else { syncStreams(); }
  renderPublicHome();
  if(position) { const cached=candleCache.get(coin+'/'+timeframe); if(cached)showCandles(cached.bars,cached.at); refreshCandles(); }
});
$('home-equity-range').addEventListener('click',event=>{
  const button=event.target.closest('[data-range]');if(!button)return;
  equityRange=button.dataset.range;renderBalance();
});
let lastCandleRequest = '', candleBusy = new Set();
let publicAccount = null, publicBusy = false;
let team = null, teamBusy = false, teamRole = '', teamMeeting = '';
const teamStateOptions = () => ({offlineMinutes:config.offlineMinutes,snapshotAt:publicAccount?.updated_at ?? snapshot?.updated_at ?? snapshot?.generated_at});
let scanExpanded=false,signalsExpanded=false,eventsBusy=false,eventsData=null,simData=null,simBusy=false;
const okxMarks=new Map();let okxMarkStream=null,okxMarkKey='',okxMarkFrame=0;   // OKX demo mark prices (see syncOkxMarks)
let scan = null, scanBusy = false, scanSort = 'quote_24h', scanDirection = -1;
function syncHomeCoin() {
  focusSymbol = meetingSymbol(team?.status.meeting, configuredSymbol, new Set([...tickers.map(t=>t.id),...(scan?.instruments || []).map(r=>r.instId)]));
  const universe = chartUniverse(publicAccount, scan, focusSymbol);
  const active = new Set(universe.choices.filter(c=>c.held || c.pending).map(c=>c.id));
  if (!universe.choices.some(c => c.id === manualHomeCoin) || previousActive.has(manualHomeCoin) && !active.has(manualHomeCoin)) manualHomeCoin = null;
  previousActive=active;
  homeCoin = manualHomeCoin || universe.defaultSymbol;
  if (route === 'home' && coin !== homeCoin) {
    coin = homeCoin; routeVersion++;
    candleCharts?.destroy(); candleCharts = null;$('chart-levels').replaceChildren();
    $('coin-title').textContent=coin.replace('-USDT-SWAP','')+' / USDT 永续';
    $('chart-error').hidden=true; $('candle-updated').textContent = '加载中';
    $('conditions').innerHTML = ''; $('signal-summary').innerHTML = '<strong>— <small>/ —</small></strong><span>等待 K 线数据</span>';
    const cached = candleCache.get(coin + '/' + timeframe);
    if (cached) showCandles(cached.bars, cached.at);
    refreshCandles();
  }
  syncStreams();updateChartOverlays();
}
function updateChartOverlays() {
  if(!candleCharts)return;
  const positions=mainAccounts(publicAccount).flatMap(a=>a.positions);
  const levels=chartLevels(positions,pendingOrders(publicAccount,scan),coin);
  candleCharts.levels(levels);
  $('chart-levels').innerHTML=levels.map(l=>`<span style="color:${l.color}">${l.title} ${price(l.price)}</span>`).join('');
  const bars=candleCache.get(coin+'/'+timeframe)?.bars || [];
  candleCharts.markers(tradeMarkers(publicAccount,coin,bars));
}
function renderScan() {
  $('scan-count').textContent = scan?.source_status === 'missing' ? '待更新' : scan ? `${scan.instruments.length} 币` : '— 币';
  $('scan-updated').textContent = scan?.generated_at ? `${time(scan.generated_at)} UTC+7 · 原生 1H 扫描` : '等待下次主力盘轮询生成 scan.json';
  const rows = scanRows(scan, $('scan-filter').value, scanSort, scanDirection);
  const coinButton = id => `<button data-chart-coin="${escape(id)}">${escape(id.split('-')[0])}</button>`;
  $('scan-body').innerHTML = rows.length ? (scanExpanded?rows:rows.slice(0,15)).map(r => `<tr><td><a class="coin-label" href="#/coin/${encodeURIComponent(r.instId)}">${coinIcon(r.instId)}${escape(r.instId.split('-')[0])}</a></td><td>${compact(r.quote_24h)} USDT</td><td>${num(r.spread === null ? null : r.spread*100,3)}%</td><td>${num(r.leverage_cap,0)}×</td><td>${escape(signalText(r))}</td><td>${r.held?'持仓':'空仓'}</td></tr>`).join('') : '<tr><td colspan="6" class="empty">暂无符合条件的币种</td></tr>';
  $('scan-expand').hidden=rows.length<=15;
  $('scan-expand').textContent=scanExpanded?'收起':'展开全部（'+rows.length+'）';
  $('scan-expand').setAttribute('aria-expanded',String(scanExpanded));
  const recent=(scan?.recent_signals || []).filter(r=>r.strategy==='manual' || r.strategy==='mean_reversion' && strategyEntries(mainAccounts(publicAccount)[0]).some(s=>s.name===r.strategy)).sort((a,b)=>Date.parse(b.timestamp)-Date.parse(a.timestamp));
  $('scan-recent').innerHTML=recent.length?(signalsExpanded?recent:recent.slice(0,10)).map(r=>`<tr><td>${time(r.timestamp)}</td><td>${coinButton(r.instId)} / ${escape(strategyName(r.strategy))}</td><td class="${r.side===1?'up':'down'}">${r.side===1?'多':'空'}</td><td>${r.opened?'已开仓':escape(reasonText(r.reason))}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">暂无均值回归或会议单信号</td></tr>';
  $('signals-expand').hidden=recent.length<=10;
  $('signals-expand').textContent=signalsExpanded?'收起':'展开（其余 '+Math.max(0,recent.length-10)+' 条）';
  $('signals-expand').setAttribute('aria-expanded',String(signalsExpanded));
  const held = chartUniverse(publicAccount, scan, focusSymbol).choices.filter(c=>c.held);
  $('scan-held').innerHTML = held.length ? held.map(c=>coinButton(c.id)).join('') : '暂无持仓';
}
async function refreshScan() {
  if (scanBusy) return; scanBusy = true;
  try {
    const response = await fetch(`./paper/scan.json?t=${Date.now()}`,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error('扫描快照读取失败');
    const data = await response.json(); if (!validScan(data)) throw new Error('扫描快照格式无效');
    scan=data;setError('scan',null);syncHomeCoin();renderScan();if(route==='home')renderHome();
  } catch {setError('scan','全市场扫描暂无有效更新');}
  finally {scanBusy=false;}
}
$('scan-expand').addEventListener('click',()=>{scanExpanded=!scanExpanded;renderScan();});
$('signals-expand').addEventListener('click',()=>{signalsExpanded=!signalsExpanded;renderScan();});
$('scan-filter').addEventListener('change',renderScan);
$('scan-table').addEventListener('click',event=>{
  const button=event.target.closest('[data-scan-sort]');if(!button)return;
  const key=button.dataset.scanSort;
  scanDirection=scanSort===key?-scanDirection:key==='instId'?1:-1;scanSort=key;
  $('scan-table').querySelectorAll('[data-scan-sort]').forEach(b=>{
    const active=b.dataset.scanSort===key;
    b.closest('th').setAttribute('aria-sort',active?(scanDirection===1?'ascending':'descending'):'none');
    b.textContent=b.textContent.replace(/ [↑↓]$/,'')+(active?(scanDirection===1?' ↑':' ↓'):'');
  });renderScan();
});
function chooseHomeCoin(id) {
  const position=homePositions.positions.find(p=>p.symbol===id);
  if(position)homePositions.toggle(position.key);else location.hash='#/coin/'+id;
}
$('scan-panel').addEventListener('click',event=>{const b=event.target.closest('[data-chart-coin]');if(b)chooseHomeCoin(b.dataset.chartCoin);});
function renderTimeline() { $('team-timeline').innerHTML = timelineMarkup(team, teamRole, teamMeeting); }
async function refreshTeam() {
  if (teamBusy) return; teamBusy = true;
  try {
    const response = await fetch(`./paper/team.json?t=${Date.now()}`, {cache:'no-store', credentials:'omit', signal:AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error('团队数据读取失败');
    const data = await response.json();
    if (!validTeam(data)) throw new Error('团队数据格式无效');
    const fresh=teamRecords.update(data.log);
    team = data; setError('team', null); syncHomeCoin();
    $('team-updated').textContent = team.status.updated_at ? `团队更新 ${teamTime(team.status.updated_at)}` : '团队待命 · 暂无更新';
    const meetings = [...new Set(team.log.map(r => r.meeting))].sort().reverse();
    if (team.status.meeting && !meetings.includes(team.status.meeting)) meetings.unshift(team.status.meeting);
    if (!meetings.includes(teamMeeting)) teamMeeting = '';
    $('team-meeting').innerHTML = '<option value="">全部会议</option>' + meetings.map(m => `<option value="${escape(m)}">${escape(m)}</option>`).join('');
    $('team-meeting').value = teamMeeting; renderTimeline(); if (route === 'home') renderHome();
    for(const row of fresh)notify(reportLine(row),`${ROLES[row.role]} · 新报告：${row.title}`);
  } catch { setError('team', '团队数据暂时无法读取'); }
  finally { teamBusy = false; }
}
$('team-role-filters').innerHTML = Object.entries({'':'全部',...ROLES}).map(([role,name]) => `<button data-team-role="${role}" class="${role ? '' : 'active'}" aria-pressed="${!role}">${name}</button>`).join('');
$('team-role-filters').addEventListener('click', event => {
  const button = event.target.closest('[data-team-role]'); if (!button) return;
  teamRole = button.dataset.teamRole;
  $('team-role-filters').querySelectorAll('button').forEach(b => { b.classList.toggle('active', b === button); b.setAttribute('aria-pressed', String(b === button)); });
  renderTimeline();
});
$('team-meeting').addEventListener('change', () => { teamMeeting = $('team-meeting').value; renderTimeline(); });

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
    const data = normalizeTickers(await api.get('market/tickers', { instType: 'SWAP' }, 10));
    if (!data.length) throw new Error('接口未返回有效 USDT 永续行情');
    const firstLoad = !tickers.length; tickers = data;
    top = data.filter(r => r.turnover !== null).sort((a, b) => b.turnover - a.turnover).slice(0, MARKET_SIZE).map((r, i) => ({ ...r, rank: i + 1 }));
    priceAt = Date.now(); setError('prices', null); updateStreamStatus();
    $('price-updated').textContent = `${time(priceAt)} UTC+7 更新`;
    if (firstLoad) syncHomeCoin();
    renderMarket(); if (route === 'coin') renderQuote(); if (route === 'home') renderHome(); if (route === 'sim' || route === 'home') renderSim(); // live PnL on paper positions
    if (['home', 'market', 'coin'].includes(route)) refreshSlow();
  } catch (error) { setError('prices', `行情：${description(error)}`); if (!tickers.length) renderMarket();if(route==='home')renderPublicHome(); }
  finally { priceBusy = false; refreshHealth(); }
}
async function refreshSlow() {
  if (slowBusy || !top.length) return;
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

function renderAnalysis(bars) {
  const main=mainAccounts(publicAccount)[0];
  const params=strategyEntries(main).some(s=>s.name==='mean_reversion')?main?.strategies?.mean_reversion:null;
  const data=analyze(bars,params);
  $('signal-summary').innerHTML=`<strong>${data.ready?data.count:'—'} <small>/ ${data.checks.length}</small></strong><span>${data.ready?'项满足 · '+(data.side===1?'多头入场':data.side===-1?'空头入场':'等待回归信号'):'等待启用策略配置或连续已收盘样本'}</span>`;
  $('signal-time').textContent=`原生 1H 为执行依据 · 当前 ${timeframe} 分析 · ${data.bar?time(data.bar.time*1000)+' UTC+7':'暂无已收盘 K 线'}`;
  $('conditions').innerHTML=data.checks.map(c=>`<li class="${c.pass?'passed':''}"><div class="condition-name">${escape(c.label)}<span class="${c.pass?'up':''}">${c.pass?'满足':'未满足'}</span></div><p>${escape(c.detail)}</p></li>`).join('');
  $('levels').innerHTML=data.levels?[20,50].map(n=>`<div class="level-row"><span>前 ${n} 根</span><span>支撑 ${price(data.levels['low'+n])}</span><span>阻力 ${price(data.levels['high'+n])}</span></div>`).join(''):'暂无区间数据';
}
function showCandles(bars, at) {
  renderAnalysis(bars);
  if (route === 'home') { $('analysis-period').textContent = `${timeframe === '1H' ? '1 小时' : '4 小时'} · 已收盘信号`; renderHome(); }
  $('candle-updated').textContent = `${bars.length} 根 · ${time(at)} 更新`;
  if (!chartAvailable()) { $('chart-error').hidden = false; $('chart-error').textContent = '图表库加载失败，请检查 CDN 连接后刷新页面。入场条件仍可查看。'; return; }
  if(route==='home' && !homePositions.expanded)return;
  try {
    if (!candleCharts) candleCharts = createCandleCharts();
    candleCharts.update(bars); updateChartOverlays(); $('chart-error').hidden = true;
  } catch { $('chart-error').hidden = false; $('chart-error').textContent = '图表暂时无法绘制，指标数据仍可查看。'; }
}
async function refreshCandles(force = false) {
  if (!['home', 'coin'].includes(route)) return;
  const id = coin, bar = timeframe, key = id + '/' + bar, version = routeVersion;
  const cached = candleCache.get(key);
  if (candleBusy.has(key) || (!force && cached && Date.now() - cached.at < 60000)) return;
  candleBusy.add(key); lastCandleRequest = key;
  try {
    const bars = parseCandles(await api.get('market/candles', { instId: id, bar, limit: 300 }, 8));
    if (!bars.length) throw new Error('接口未返回有效 K 线');
    const at = Date.now(); candleCache.set(key, { bars, at });
    if (route === 'home') renderHome();
    if (!['home', 'coin'].includes(route) || coin !== id || timeframe !== bar) return;
    // 同一合约被离开又进入时也可使用刚完成的数据。
    if (version !== routeVersion && lastCandleRequest !== key) return;
    setError('candles', null); showCandles(bars, at);updateStreamStatus();
  } catch (error) { if (['home', 'coin'].includes(route) && coin === id && timeframe === bar) setError('candles', `K 线：${description(error)}`); }
  finally { candleBusy.delete(key); }
}

async function refreshSnapshot() {
  if (snapshotBusy) return;
  snapshotBusy = true;
  try {
    const response = await fetch(`./paper/snapshot.json?t=${Date.now()}`, { cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(12000) });
    if (!response.ok) throw new Error('接口未找到快照，请先生成 paper/snapshot.json');
    const data = await response.json();
    if (!validSnapshot(data)) throw new Error('接口快照格式不完整');
    const changed = snapshot?.generated_at !== data.generated_at;
    snapshot = {...data, accounts:data.accounts.filter(a => ['paper_main','paper_1h','paper_4h','paper_1h_aggr','paper_4h_aggr'].includes(a.id))}; setError('snapshot', null);

    if (route === 'home') { renderHome(); if (changed) renderEquity(); }

    refreshHealth();
  } catch (error) { setError('snapshot', `模拟盘：${description(error)}`); }
  finally { snapshotBusy = false; }
}
function renderEquity() { if(route==='home')renderBalance(); }
function refreshHealth() {
  renderPinnedBalance();updateStreamStatus();
  const healthy = priceAt && Date.now() - priceAt < 24000 && !errors.has('prices');
  if (!snapshot) return;
  const staleGenerated = Date.now() - Date.parse(snapshot.generated_at) > 15 * 60000;
  const staleSource = snapshot.accounts.filter(a => a.status !== '已停止').some(a => !a.updated_at || Date.now() - Date.parse(a.updated_at) > 15 * 60000);
  if (publicAccount) return;
  $('home-snapshot-status').textContent = `${staleGenerated || staleSource ? '数据可能延迟 · ' : ''}快照生成 ${time(snapshot.generated_at)} UTC+7`;
  $('home-snapshot-status').className = staleGenerated || staleSource ? 'stale' : 'muted';
}
function renderHome() {
  // Keep the original snapshot metrics for the legacy account route. The home
  // view is authoritative from account.json, including future live snapshots.
  renderPublicHome();
}
async function refreshPublicAccount() {
  if (publicBusy) return;
  publicBusy=true;
  try {
    const response=await fetch(`./paper/account.json?t=${Date.now()}`,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error('公开账户快照读取失败');
    const data=await response.json();
    if (!validAccount(data)) throw new Error('公开账户快照格式无效');
    const changed=publicAccount?.updated_at !== data.updated_at || publicAccount?.mode !== data.mode;
    const freshFills=fillRecords.update(mainAccounts(data).flatMap(a=>a.fills));
    publicAccount=data;renderPinnedBalance();
    if(['home','coin'].includes(route)){const cached=candleCache.get(coin+'/'+timeframe);if(cached)renderAnalysis(cached.bars);}
    for(const fill of freshFills){const line=fillLine(fill);notify(line,`新成交 · ${line.text}`);}
    setError('account',null);syncHomeCoin();renderScan();
    if(route==='home'){renderHome();renderBalance();} else if(route==='coin')renderDecisions();
  }catch{setError('account','账户快照暂无有效更新');renderPinnedBalance();}
  finally{publicBusy=false;}
}
function renderBalance() {
  if(route!=='home')return;
  const points=rangePoints(aggregateCurve(publicAccount?.accounts || []),equityRange);
  $('equity-chart').innerHTML=equitySVG(points);
  $('equity-error').hidden=Boolean(points.length);
  const pause=publicAccount?.pause;
  $('equity-error').textContent=pause?.paused && pause.reset?`暂无记录 · 重新开始后从 ${num(pause.next_initial_equity)} USDT 起记录资金曲线`:'暂无记录';
  $('home-equity-range').querySelectorAll('button').forEach(b=>{
    const active=b.dataset.range===equityRange;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));
  });
}
function renderPublicHome() {
  const pause=publicAccount ? publicAccount.pause : snapshot?.accounts.find(a=>a.id==='paper_main')?.pause;
  const banner=$('pause-banner');banner.hidden=!pause?.paused;
  if(pause?.paused)banner.innerHTML=`<strong>${escape(pause.message)}</strong><small>since ${escape(time(pause.since))} UTC+7 · ${pause.reset?'重置权益':'最终权益'} ${num(pause.reset?pause.next_initial_equity:mainAccounts(publicAccount)[0]?.equity ?? pause.final_equity)} USDT</small>`;
  const resolved=mainAccounts(publicAccount).flatMap(a=>a.positions).map(p=>({id:p.symbol,last:quoteFor(p,liveQuotes,tickers,errors.has('prices')?0:priceAt).mark}));
  const summary=summarizeAccounts(publicAccount,resolved);
  const mode=publicAccount?.mode==='live'?'实盘':'模拟盘';
  $('execution-metrics').innerHTML=`<span>今日成交 <b>${summary?num(summary.today.length,0)+' 笔':'—'}</b></span><span>今日成交额 <b>${compact(summary?.turnover)} USDT</b></span><span>持仓名义金额 <b>${compact(summary?.size)} USDT</b></span>`;
  $('mode-tag').textContent=mode;$('execution-mode').textContent=mode;
  renderPinnedBalance();
  const current=liveBalance(publicAccount,effectiveQuotes());
  $('total-pnl').innerHTML=`当日盈亏 <span class="${tone(current?.daily)}">${signed(current?.daily)} USDT / ${signed(current?.percent)}%</span>`;
  const stale=publicAccount && Date.now()-Date.parse(publicAccount.updated_at)>config.offlineMinutes*60000;
  const sourceStale=summary?.accounts.filter(({account:a})=>a.status!=='暂停').some(({account:a})=>!a.updated_at || Date.now()-Date.parse(a.updated_at)>config.offlineMinutes*60000);
  $('home-snapshot-status').textContent=publicAccount?`${stale || sourceStale?'数据可能延迟 · ':''}${time(publicAccount.updated_at)} UTC+7`:'暂无快照时间';
  $('home-snapshot-status').className=stale || sourceStale?'stale':'muted';
  const positions=mainAccounts(publicAccount).filter(a=>a.status!=='未启动').flatMap(a=>a.positions.map(p=>{
    const quote=a.status==='已停止'?{mark:p.mark_price,source:'快照标记价 · 已停止'}:quoteFor(p,liveQuotes,tickers,errors.has('prices')?0:priceAt);
    return {...p,...positionAmounts(p,quote.mark),source:quote.source};
  }));
  $('live-position-count').textContent=summary?positions.length+' 仓':'—';
  const cell=(label,value,cls='')=>`<td data-label="${label}" class="${cls}">${value}</td>`;
  homePositions.render(positions,pause?.paused);
  $('recent-fills').innerHTML=summary?.fills.length?summary.fills.slice(0,8).map(f=>`<li><div><a class="coin-label" href="#/coin/${encodeURIComponent(f.symbol)}">${coinIcon(f.symbol)}${escape(f.symbol.replace('-USDT-SWAP',''))}</a><span class="${f.side===1?'up':f.side===-1?'down':''}">${f.side===1?'多':f.side===-1?'空':'—'} / ${f.kind==='open'?'开仓':'平仓'}</span><time>${time(f.timestamp)}</time></div><div><span>${num(f.qty,4)} × ${price(f.price)}</span><span class="${tone(f.pnl)}">${f.kind==='close'?signed(f.pnl)+' USDT':'成交额 '+compact(f.notional)}</span></div></li>`).join(''):'<li class="empty">暂无成交</li>';
  const accounts=mainAccounts(publicAccount);
  const main=accounts[0];
  const manual=manualOrders(publicAccount,scan);
  const maker=pendingOrders(publicAccount,scan).filter(r=>r.kind==='maker');
  const orders=[...manual.map(r=>({...r,kind:'manual'})),...maker];
  $('manual-orders-panel').hidden=!orders.length;
  $('manual-orders').innerHTML=orders.map(r=>{const amounts=orderAmounts(r);return `<tr data-order="${escape(r.symbol)}"><td class="position-name">${escape(r.symbol?.replace('-USDT-SWAP','') || '—')}<span class="side ${r.side===1?'up':r.side===-1?'down':''}">${r.side===1?'多':r.side===-1?'空':'—'}</span><small class="position-source">${r.kind==='maker'?'maker 挂单':'会议单'} · ${escape(r.status || 'pending')}</small></td>${cell('名义价值 (USDT)',num(amounts.notional))}${cell('计划风险 (USDT)',num(amounts.risk))}${cell('限价',price(r.limit))}${cell('止损 / 止盈',`${price(r.stop)} / ${price(r.take_profit)}`)}${cell('有效期 UTC+7',time(r.valid_until))}${cell('数量 (币)',num(baseQuantity(r),6)+(amounts.notional===null?'<small class="quote-source">数量待确定</small>':''))}</tr>`;}).join('');
  $('enabled-strategies').innerHTML=main?strategyEntries(main).map(({name,mode})=>`<div>${escape(strategyName(name))} <b>${escape(mode || '入场方式未提供')}</b></div>`).join('') || '暂无启用策略':'等待主力盘快照';
  $('strategy-states').innerHTML=strategyEntries(main).map(({name})=>`<div class="strategy-row"><span>${escape(strategyName(name))}</span><span class="up">${escape(summary?.accounts[0]?.state || '等待状态')}</span><input type="checkbox" role="switch" aria-label="${escape(strategyName(name))}，只读" ${main?.strategy_enabled!==false?'checked':''} disabled></div>`).join('') || `<p class="empty">${pause?.paused?'暂停':'暂无启用策略'}</p>`;
  $('daily-risk').innerHTML=summary?summary.accounts.map(({account:a,loss})=>{
    if(!Number.isFinite(a.daily_loss_limit))return '<p class="empty">暂无风控数据，重新开始后显示</p>';
    const remaining=loss===null?null:Math.max(0,a.daily_loss_limit-loss);
    const percent=loss===null?0:Math.min(100,loss/a.daily_loss_limit*100);
    return `<div class="loss-item"><div><span>${escape(a.name || '策略')} · 日亏停手线 ${num(a.daily_loss_limit*100,0)}%</span><strong class="${remaining===0?'down':'up'}">${remaining===null?'—':num(remaining*100)+'%'} <small>剩余</small></strong></div><progress max="100" value="${percent}" aria-label="当日亏损额度使用比例" ${loss===null?'data-missing="true"':''}></progress><small>当日亏损 ${loss===null?'—':num(loss*100)+'%'}${loss===null?' · 暂无当日基准':''}</small></div>`;
  }).join(''):'暂无当日亏损数据';
  $('room-risk-params').innerHTML=summary?summary.accounts.map(({account:a,loss})=>{
    const r=a.risk;
    return `<article class="risk-config"><h3>${escape(a.name || '策略')}</h3>${r?`<div class="risk-config-grid"><span>单笔风险 <b>${num(r.risk_per_trade*100)}%</b></span><span>同向风险 <b>${Number.isFinite(r.same_direction_risk_limit)?num(r.same_direction_risk_limit*100)+'%':'—'}</b></span><span>逐仓杠杆 <b>${r.leverage_tiers?num(r.leverage_tiers.turnover_gte_50000000,0)+'× / '+num(r.leverage_tiers.turnover_lt_50000000,0)+'×':Number.isFinite(r.leverage)?num(r.leverage,0)+'×':'—'}</b></span><span>日亏上限 <b>${num(a.daily_loss_limit*100)}%</b></span></div>`:'<p class="muted">暂无风控细项</p>'}<small>当日亏损 ${loss===null?'—':num(loss*100)+'%'}</small></article>`;
  }).join(''):'<p class="empty">暂无实际风控参数</p>';
  const strategyNames={breakout:'突破放量',mean_reversion:'均值回归',ema:'EMA 趋势'};
  const paramNames={fast:'快线',slow:'慢线',lookback:'回看',volume:'成交额倍数',rsi:'RSI 阈值',trend:'趋势距离'};
  $('strategy-params').innerHTML=accounts.map(a=>Object.entries(a.strategies || {}).filter(([key])=>strategyEntries(a).some(s=>s.name===key)).map(([key,values])=>`<div class="strategy-parameters"><h3>${escape(a.name || '策略')} / ${strategyNames[key] || '策略'}</h3><dl class="thresholds">${Object.entries(values).filter(([k])=>Object.hasOwn(paramNames,k)).map(([k,v])=>`<div><dt>${paramNames[k]}</dt><dd>${num(v,Number.isInteger(v)?0:3)}</dd></div>`).join('')}</dl></div>`).join('')).join('') || '<p class="empty">暂无策略参数</p>';
  renderDecisions();
}
function renderDecisions() {
  const d=publicAccount?.decisions;
  const bias={long:'偏多',short:'偏空',watch:'观察',ban:'禁开仓'};
  const rule=d?.symbols?.[coin];
  $('coin-decision').innerHTML=`<span class="eyebrow">MEETING / 会议结论 · ${escape(coin.replace('-USDT-SWAP',''))}</span><strong>${rule?bias[rule.bias] || '—':'暂无'}</strong><p>${escape(rule?.note || '暂无会议结论')}</p>`;
  if(!d || d.status==='missing'){$('decisions-panel').innerHTML='<p class="muted">暂无决议</p><p class="muted">事件窗口：暂无</p>';return;}
  const windows=d.blackout_windows.filter(w=>Date.parse(w.end)>Date.now());
  $('decisions-panel').innerHTML=`<p>${escape(d.meeting || '暂无会议名称')} · 版本 ${escape(d.version || '—')}</p><small>${time(d.updated_at)} UTC+7</small><p>全局仓位系数 ${num(d.global_size_factor)}</p>${Object.entries(d.symbols).map(([symbol,r])=>`<article><strong>${escape(symbol.replace('-USDT-SWAP',''))} · ${bias[r.bias] || '—'}</strong><small>仓位系数 ${num(r.size_factor)}</small><p>${escape(r.note || '暂无')}</p></article>`).join('') || '<p>暂无币种决议</p>'}<h3>事件窗口</h3>${windows.length?windows.map(w=>`<article><span class="${Date.parse(w.start)<=Date.now()?'down':'muted'}">${Date.parse(w.start)<=Date.now()?'生效中':'待生效'} · ${escape(w.symbols.join(' / '))}</span><p>${time(w.start)} → ${time(w.end)} UTC+7</p><small>${escape(w.reason || '暂无原因')}</small></article>`).join(''):'<p class="muted">暂无</p>'}`;
}

function navigate() {
  routeVersion++;
  if (['#/paper', '#/sim'].includes(location.hash)) { location.replace('#/'); return; }   // 账户历史 and 模拟交易 were merged into 交易室
  const hash = location.hash || '#/';
  const match = /^#\/coin\/([A-Z0-9]+-USDT-SWAP)$/.exec(hash);
  route = match ? 'coin' : hash === '#/market' ? 'market' : hash === '#/evolve' ? 'evolve' : 'home';
  coin = match?.[1] || homeCoin;
  candleCharts?.destroy(); candleCharts = null;
  for (const page of ['home', 'market', 'coin', 'evolve']) $(`${page}-page`).hidden = route !== page;
  document.querySelectorAll('[data-nav]').forEach(a => { const active = a.dataset.nav === (route === 'coin' ? 'market' : route); a.classList.toggle('active', active); if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  $('home-price-slot').append($('price-panel'));
  $('home-analysis-slot').append($('analysis-content'));
  if(route==='home')homePositions.mount();

  if (route === 'market') $('market-page').append($('market-content'));
  renderMarket();
  if (route === 'coin') { $('coin-price-slot').append($('price-panel')); $('coin-analysis-slot').append($('analysis-content')); }

  document.querySelectorAll('[data-timeframe]').forEach(b => { const active = b.dataset.timeframe === timeframe; b.classList.toggle('active', active); b.setAttribute('aria-pressed', String(active)); });
  setError('candles', null);
  document.title = route === 'coin' ? `${coin.split('-')[0]} · MODI 交易团队` : route === 'sim' ? '模拟交易 · MODI 交易团队' : route === 'evolve' ? '策略进化 · MODI 交易团队' : 'MODI · AI 交易团队';
  if (['home', 'coin'].includes(route)) {
    $('coin-title').textContent = coin.replace('-USDT-SWAP', '') + ' / USDT 永续'; renderQuote();
    $('signal-summary').innerHTML = '<strong>— <small>/ —</small></strong><span>等待 K 线数据</span>';
    $('conditions').innerHTML = ''; $('signal-time').textContent = '仅分析最后一根已收盘 K 线'; $('levels').innerHTML = '<p class="muted">等待 K 线数据</p>'; $('candle-updated').textContent = '加载中'; $('chart-error').hidden = true;
    renderDecisions();
    const cached = candleCache.get(coin + '/' + timeframe); if (cached) showCandles(cached.bars, cached.at);
    refreshCandles(); refreshSlow();
    if (route === 'home') { syncHomeCoin(); refreshTeam(); refreshScan(); renderHome(); if (snapshot) renderEquity(); }
  } else { renderMarket(); refreshSlow(); }
  if (route === 'home') refreshSnapshot();
  if(route==='evolve')queueMicrotask(()=>geneFx.kick());   // resume the gene-pool motion (geneFx is created later in this module)
  document.documentElement.dataset.route=route;renderPinnedBalance();renderSim();refreshSim();refreshEvolve();
  refreshPublicAccount();syncStreams();
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
  timeframe = button.dataset.timeframe;
  document.querySelectorAll('[data-timeframe]').forEach(b => { b.classList.toggle('active', b === button); b.setAttribute('aria-pressed', b === button); }); navigate();
}));
$('reset-chart').addEventListener('click',()=>candleCharts?.reset());
window.addEventListener('hashchange',navigate);
window.addEventListener('load',()=>{if(['home','coin'].includes(route)){const cached=candleCache.get(coin+'/'+timeframe);if(cached)showCandles(cached.bars,cached.at);}if(route==='home')renderBalance();});
function effectiveQuotes(){
  const quotes=new Map();
  for(const p of mainAccounts(publicAccount).flatMap(a=>a.positions)){
    const q=quoteFor(p,liveQuotes,tickers,errors.has('prices')?0:priceAt);
    if(!q.source.startsWith('快照') && Number.isFinite(q.mark))quotes.set(p.symbol,{price:q.mark,at:Date.now()});
  }
  return quotes;
}
function renderPinnedBalance(){
  const current=liveBalance(publicAccount,effectiveQuotes());
  if(route==='home')rollNumber($('total-equity'),current?.equity);
  // One writer per frame: writing the paused bot's 100.00 and then the paper equity made the
  // number change twice on every refresh, so it animated even when nothing had moved.
  if(simPrimary()){if(simData)renderPinnedSim();else renderStatusPill();}
  else {
    $('pinned-label-text').textContent='主力盘余额';
    rollNumber($('pinned-equity'),current?.equity);
    $('pinned-mode').textContent=publicAccount?(publicAccount.mode==='live'?'实盘':'模拟盘'):'—';
    $('pinned-daily').textContent=current?.daily!=null?`当日 ${signed(current.daily)} / ${signed(current.percent)}%`:'当日盈亏 · 暂无当日起始权益';
    $('pinned-daily').className=tone(current?.daily);
    const pctEl=$('pinned-pct');if(pctEl){pctEl.textContent=current?.percent!=null?`${signed(current.percent)}%`:'—';pctEl.className=`pinned-pct ${tone(current?.percent)}`;}
    $('pinned-source').textContent=current?.source || '暂无账户数据';
    const stale=publicAccount && (isStale(publicAccount.updated_at,Date.now(),config.offlineMinutes) || mainAccounts(publicAccount).filter(a=>!['未启动','已停止','暂停'].includes(a.status)).some(a=>isStale(a.updated_at,Date.now(),config.offlineMinutes)));
    $('pinned-time').textContent=publicAccount?`${stale?'延迟 · ':''}${time(publicAccount.updated_at)} UTC+7`:'— UTC+7';
    $('pinned-time').className=stale?'stale':'';
  }
  updateCompanion(current);
}
function renderPinnedSim(){
  const t=okxTotals(simData,okxMarks),stale=Date.now()-Date.parse(simData.updated_at)>OKX_STALE;
  $('pinned-label-text').textContent='OKX 模拟权益';$('pinned-mode').textContent='交易所模拟';
  rollNumber($('pinned-equity'),t.equity,EQUITY_ROLL_STEP);
  const pct=Number.isFinite(t.dayPct)?t.dayPct:t.ret;
  const el=$('pinned-pct');
  if(el){el.textContent=Number.isFinite(pct)?`${signed(pct)}%`:'—';el.className=`pinned-pct ${tone(pct)}`;}
  const odd=okxOutlier(simData,t);   // one outsized trade: shown on the home card, not in the header
  $('pinned-daily').textContent=`今日 ${Number.isFinite(t.day)?`${signed(t.day)}（${signed(t.dayPct)}%）`:'—'} · ${odd?`累计 ${signed(odd.retWithout)}%（扣单笔 ${odd.inst.split('-')[0]}）`:`累计 ${signed(t.ret)}%`}${stale?' · 延迟':''}`;$('pinned-daily').className=tone(Number.isFinite(t.day)?t.day:t.ret);
  $('pinned-source').textContent='OKX 模拟交易 · 约每 4 小时更新';
  $('pinned-time').textContent=`${stale?'延迟 · ':''}${time(simData.updated_at)} UTC+7`;$('pinned-time').className=stale?'stale':'';
  renderStatusPill();
}
function renderStatusPill(){
  const pill=$('status-pill'), label=$('status-pill-label'), body=$('status-pill-body');
  if(!pill||!label||!body)return;
  const src=simPrimary()&&simData?simData:null;
  const sched=src?updateSchedule(src.updated_at):null;
  const demoView=['home','sim'].includes(route)&&simPrimary()&&!showLegacy;
  // Home's live view is the paper-data OKX / evolve feeds. Errors from the retired paper/*.json
  // snapshots (11/2 主力盘) must not paint warning banners on the trading terminal.
  const hideOnHome=new Set(['prices','oi','funding','candles','account','snapshot','team','scan','events']);
  const list=[...errors].filter(([k])=>!(demoView&&hideOnHome.has(k))).map(([,v])=>v);
  const stale=!!sched?.stale;
  const failed=list.length>0;
  label.textContent=failed?`● ${list.length} 项读取失败`: (sched?sched.pill:'● 等待更新');
  pill.classList.toggle('stale', stale && !failed);
  pill.classList.toggle('error', failed);
  const extra=[];
  if(sched) extra.push(sched.line+(sched.warn?` ${sched.warn}`:''));
  if(src) extra.push(`行情与账户快照来自 paper-data，定时 ${'17 分 / 每 4 小时'}（UTC，金边时间显示）。GitHub Actions 可能推迟。`);
  if(list.length) extra.push('读取失败：'+list.join('；')+'。已有数据保留，自动重试。');
  body.innerHTML=extra.map(s=>`<p>${escape(s)}</p>`).join('')||'<p>正在读取账户快照…</p>';
  // Do not surface the retired 主力盘 pause banner on the OKX-primary home.
  const banner=$('pause-banner');
  if(banner && simPrimary() && !showLegacy) banner.hidden=true;
}
const BIAS={long:'偏多',short:'偏空',watch:'观察',ban:'禁开仓'};
function updateCompanion(current=liveBalance(publicAccount,effectiveQuotes())){
  const main=mainAccounts(publicAccount)[0],loss=summarizeAccounts(publicAccount)?.accounts[0]?.loss ?? null;
  const risk=main && main.status!=='未启动'?riskProgress(main):null,rule=publicAccount?.decisions?.symbols?.[coin];
  mascot.update({...publicAccount,dataDisconnected:errors.has('account'),now:Date.now(),offlineMinutes:config.offlineMinutes,events:eventsData?.events || []},team,{
    route,coin,decisions:{summary:simData,history:decisionHistory,status:decisionHistoryStatus},paused:Boolean(publicAccount?.pause?.paused),equity:current?.equity,daily:current?.daily,percent:current?.percent,source:current?.source,
    positions:main?main.positions.length:null,loss:Number.isFinite(main?.daily_loss_limit)?{ratio:loss,limit:main.daily_loss_limit}:null,
    long:risk?.long,short:risk?.short,decision:rule?BIAS[rule.bias] || null:null,eventText,...simContext()});
}
// Once the OKX demo account has loaded, the companion follows it instead of the paused bot snapshot.
// What the companion is doing (watching / scanning / waiting), its PnL mood and the double-tap report.
const pnlMemo={upl:null,at:0};
function companionWork(){
  if(!simData)return null;
  const l=okxLive(simData,okxMarks),positions=l.positions.map(p=>({inst:p.inst,side:p.side,upl:p.upl,ours:p.ours}));
  const work=workState({positions,equity:l.equity,paused:Boolean(simData.paused),halted:Boolean(simData.halted),offline:Date.now()-Date.parse(simData.updated_at)>OKX_STALE});
  return {work,mood:pnlMood(work.upl,l.equity),positions,equity:l.equity};
}
function updateCompanionWork(){
  const cw=companionWork();if(!cw)return;
  mascot.context.report=positionReport(cw.positions);mascot.setWork(cw.work,cw.mood);
  if(mascot.floating){const t=okxTotals(simData,okxMarks);mascot.setPipInfo({equity:`${num(t.equity)} U`,line:`今日 ${Number.isFinite(t.day)?signed(t.day):'—'} · ${cw.work.label}`,tone:tone(Number.isFinite(t.day)?t.day:t.ret)});}
  const c=pnlComment(pnlMemo,{upl:cw.work.upl,equity:cw.equity,positions:cw.positions});if(c)mascot.say(c.text,c.mood);
}
function simContext(){
  if(!simData)return {};
  const t=okxTotals(simData,okxMarks);
  return {report:positionReport(okxLive(simData,okxMarks).positions),simState:okxMascotState(simData),simFeed:okxFeed(simData),sim:{totals:t,
    accounts:[{name:'OKX 模拟账户',color:OKX_COLOR,equity:t.equity,return_pct:t.ret,positions:t.positions,halted:Boolean(simData.halted)}],strategies:[]}};
}
const ago=ms=>ms?`${Math.round((Date.now()-ms)/1000)} 秒前`:'从未';
function diagText(){
  const ws=s=>s?`${s.state} · ${s.activeUrl||s.url} · 最近数据 ${ago(s.lastData)} · 连续失败 ${s.failures}`:'未启动';
  const marks=[...okxMarks].map(([k,v])=>`${k.split('-')[0]} ${v.price}（${ago(v.at)}）`).join('，') || '无';
  return [`版本：${BUILD}`,`时间：${new Date().toISOString()}`,`页面可见：${!document.hidden} · 路由：${route}`,`浏览器：${navigator.userAgent}`,
    `行情 REST：最近成功 ${ago(priceAt)} · 正在请求 ${priceBusy} · 合约数 ${tickers.length}`,`当前报错：${[...errors.values()].join(' / ') || '无'}`,
    `真实盘行情 WS（${quoteStreamKey || '无订阅'}）：${ws(quoteStream)}`,`模拟盘标记价 WS（${okxMarkKey || '无订阅'}）：${ws(okxMarkStream)}`,`模拟盘标记价：${marks} · REST 备用最近成功 ${ago(okxMarkPollAt)}`,
    `OKX 模拟账户数据：${simData?simData.updated_at+' · 权益 '+simData.equity:'未读取'}`,'最近事件：',...(diagLog.length?diagLog:['（无）'])].join('\n');
}
function openDiag(){$('diag-body').textContent=diagText();$('diag').hidden=false;$('diag-close').focus();}
$('connection').addEventListener('click',openDiag);
$('diag-close').addEventListener('click',()=>{$('diag').hidden=true;$('connection').focus();});
$('diag').addEventListener('click',e=>{if(e.target.id==='diag')$('diag').hidden=true;});
$('diag-copy').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(diagText()+'\n'+$('diag-test-out').textContent);$('diag-copy').textContent='已复制';}catch{$('diag-copy').textContent='复制失败，请长按选取';}});
$('diag-reconnect').addEventListener('click',()=>{
  quoteStream?.stop();candleStream?.stop();okxMarkStream?.stop();quoteStreamKey='';candleStreamKey='';okxMarkKey='';okxMarkStream=null;okxMarkPollAt=0;
  diagNote('用户主动重新连接行情');syncStreams();syncOkxMarks();pollOkxMarks();refreshPrices();refreshSim();
  $('diag-body').textContent=diagText();
});
// Direct probes that bypass the app's request queue and streams.
function probeSocket(url,instId){
  return new Promise(resolve=>{
    const t0=Date.now();let ws,done=false;const end=msg=>{if(done)return;done=true;try{ws.close();}catch{}resolve(`${msg}（${Date.now()-t0} ms）`);};
    try{ws=new WebSocket(url);}catch(e){return resolve(`无法创建：${e.message}`);}
    const timer=setTimeout(()=>end('8 秒内没有数据'),8000);
    ws.onopen=()=>ws.send(JSON.stringify({op:'subscribe',args:[{channel:'mark-price',instId}]}));
    ws.onmessage=e=>{const b=(()=>{try{return JSON.parse(e.data);}catch{return {};}})();if(b.event==='error'){clearTimeout(timer);end(`返回错误 ${b.code} ${b.msg}`);}else if(b.data?.[0]?.markPx){clearTimeout(timer);end(`成功，标记价 ${b.data[0].markPx}`);}};
    ws.onerror=()=>{clearTimeout(timer);end('连接出错（onerror）');};
    ws.onclose=e=>{clearTimeout(timer);end(`连接关闭 code ${e.code}${e.reason?' '+e.reason:''}`);};
  });
}
async function probeRest(url,headers={}){
  const t0=Date.now();
  try{const r=await fetch(url,{headers,cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(8000)});const text=await r.text();return `HTTP ${r.status} · ${text.slice(0,80)}（${Date.now()-t0} ms）`;}
  catch(e){return `失败：${e.name} ${e.message}（${Date.now()-t0} ms）`;}
}
$('diag-test').addEventListener('click',async()=>{
  const out=$('diag-test-out');out.textContent='测试中…（最多约 10 秒）';
  const [rest,demoRest,live,demo,backup]=await Promise.all([probeRest('https://www.okx.com/api/v5/public/time'),probeRest('https://www.okx.com/api/v5/public/mark-price?instType=SWAP&instId=XRP-USDT-SWAP',{'x-simulated-trading':'1'}),probeSocket('wss://ws.okx.com:8443/ws/v5/public','XRP-USDT-SWAP'),probeSocket(OKX_DEMO_PUBLIC_WS,'XRP-USDT-SWAP'),probeSocket(OKX_DEMO_FALLBACK_WS,'XRP-USDT-SWAP')]);
  out.textContent=`REST www.okx.com：${rest}\n模拟盘 REST：${demoRest}\n真实盘 WS ws.okx.com：${live}\n模拟盘 WS wspap.okx.com：${demo}\n模拟盘 WS 8443 备用：${backup}`;
  $('diag-body').textContent=diagText();
});
function updateStreamStatus(){
  const live=candleStream?.state==='实时' && Date.now()-candleStream.lastData<45000;

  // Live when any price WebSocket (exchange quotes or the OKX demo mark prices) pushed recently; polling when the
  // 8-second REST refresh is fresh. Recomputed on every refresh, so it cannot stick on a stale value.
  const quotesLive=[quoteStream,okxMarkStream].some(s=>s?.state==='实时' && Date.now()-s.lastData<45000),polling=priceAt && Date.now()-priceAt<24000;
  const demoView=simData&&(route==='sim'||route==='home'&&simPrimary());
  const demo=demoQuoteState(simData,okxMarks,okxMarkStream,okxMarkPollAt);
  const connected=demoView?demo.live||demo.polling:quotesLive||polling;
  $('connection-text').textContent=demoView?demo.label:quotesLive?'行情实时':polling?'行情轮询':'行情断开';
  $('connection-dot').className='dot '+(connected?'live':demoView&&!demo.needsQuotes?'':'error');
  $('connection').title=(demoView?'OKX 模拟行情':'市场行情')+'连接状态；点击查看连接诊断。账户开关和快照更新时间另行展示。';
  document.documentElement.classList.toggle('has-live-state',quotesLive || live || priceAt && Date.now()-priceAt<24000 || Object.keys(ROLES).some(r=>roleState(team,r,teamStateOptions()).state!=='offline'));
}
// Ticker and mark-price pushes arrive several times per second; render once per frame.
let liveRenderPending=false;
function scheduleLiveRender(){
  if(liveRenderPending)return;liveRenderPending=true;
  requestAnimationFrame(()=>{
    liveRenderPending=false;
    renderPinnedBalance();if(route==='home')renderPublicHome();if(route==='coin')renderQuote();
  });
}
function syncStreams(){
  if(document.hidden)return;
  const symbols=[...new Set([coin,...pendingOrders(publicAccount,scan).map(p=>p.symbol),...mainAccounts(publicAccount).filter(a=>!['未启动','已停止','暂停'].includes(a.status)).flatMap(a=>a.positions.map(p=>p.symbol))])].filter(Boolean).sort();
  const key=symbols.join(',');
  if(key!==quoteStreamKey){
    quoteStream?.stop();quoteStreamKey=key;
    quoteStream=new PublicStream({url:'wss://ws.okx.com:8443/ws/v5/public',args:symbols.flatMap(instId=>[{channel:'tickers',instId},{channel:'mark-price',instId}]),maxFailures:config.wsMaxFailures,
      onState:(state,reason)=>{diagNote(`真实盘行情 WS：${state}${reason?' · '+reason:''}`);if(state!=='实时')liveQuotes.clear();renderPinnedBalance();if(route==='home')renderPublicHome();updateStreamStatus();},onData:body=>{
        const id=body.arg?.instId;if(!symbols.includes(id))return false;
        const row=body.data[0],value=number(row.markPx ?? row.last),at=number(row.ts);
        if(!(value>0) || !at || Date.now()-at>45000 || at>Date.now()+60000)return false;
        const previous=liveQuotes.get(id);if(previous && previous.at>at)return false;
        liveQuotes.set(id,{price:value,at});
        if(body.arg?.channel==='tickers'){const ticker=tickers.find(t=>t.id===id);if(ticker)ticker.last=value;}
        scheduleLiveRender();
        return true;
      }});quoteStream.start();
  }
  const candleKey=route==='coin' || route==='home' && homePositions.expanded?coin+'/'+timeframe:'';
  if(candleKey!==candleStreamKey){
    candleStream?.stop();candleStreamKey=candleKey;
    if(candleKey){const id=coin,bar=timeframe;
      candleStream=new PublicStream({url:'wss://ws.okx.com:8443/ws/v5/business',args:[{channel:'candle'+bar,instId:id}],maxFailures:config.wsMaxFailures,
        onState:(state,reason)=>{streamReason=reason;updateStreamStatus();},onData:body=>{
          if(body.arg?.instId!==id || body.arg?.channel!=='candle'+bar)return false;
          const next=parseCandles(body.data.filter(Array.isArray)).at(-1);if(!next || Date.now()/1000-next.time>(bar==='1H'?7200:28800))return false;
          const cached=candleCache.get(candleKey);if(!cached)return false; // REST seeds history first.
          const bars=mergeCandle(cached.bars,next),at=Date.now();candleCache.set(candleKey,{bars,at});
          if(coin===id && timeframe===bar){renderAnalysis(bars);if(chartAvailable() && (route==='coin' || homePositions.expanded)){
            if(!candleCharts)candleCharts=createCandleCharts();candleCharts.update(bars);updateChartOverlays();
          }$('candle-updated').textContent=`${bars.length} 根 · ${time(at)} 实时更新`;}
          return true;
        }});candleStream.start();
    }
  }
}
async function refreshEvents(){
 if(eventsBusy)return;eventsBusy=true;
 try{
  const response=await fetch(`./paper/events.json?t=${Date.now()}`,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(12000)});
  if(!response.ok)throw new Error();const data=await response.json();if(!validEvents(data))throw new Error();
  data.events=data.events.filter(r=>!r.strategy || r.strategy==='manual' || r.strategy==='mean_reversion' && strategyEntries(mainAccounts(publicAccount)[0]).some(s=>s.name===r.strategy));
  const fresh=eventRecords.update(data.events),keys=new Set(fresh.map(recordKey));
  if(JSON.stringify(data.events)!==JSON.stringify(eventsData?.events)){
   const list=$('events-terminal'),nearBottom=list.scrollTop+list.clientHeight>=list.scrollHeight-40;
   list.replaceChildren();
   if(!data.events.length)list.innerHTML='<li class="empty">暂无事件</li>';
   for(const event of data.events){
    const li=document.createElement('li');li.className='event-'+event.type;
    const clock=document.createElement('time');clock.dateTime=event.ts;clock.textContent=time(event.ts);
    const line=document.createElement('span'),message=document.createElement('span');line.append(message);li.append(clock,line);list.append(li);
    if(keys.has(recordKey(event)))typeLine(message,eventText(event));else message.textContent=eventText(event);
    if(event.source_url){const link=document.createElement('a');link.href=event.source_url;link.textContent='来源 ↗';link.target='_blank';link.rel='noopener noreferrer';line.append(' ',link);}
   }
   if(!eventsData || nearBottom)list.scrollTop=list.scrollHeight;
  }
  eventsData=data;if(route==='home')renderPublicHome();$('events-count').textContent=data.events.length+' 条';
  $('events-status').textContent=data.source_status==='missing'?'暂无事件源':`${isStale(data.generated_at,Date.now(),config.offlineMinutes)?'事件快照延迟 · ':''}${time(data.generated_at)} UTC+7 · 每 60 秒同步`;
  for(const event of fresh){
   // Opens and closes are narrated from fills; events add wake alerts, halts and liquidations.
   const line=eventLine(event);
   if(line)notify(line,eventText(event));
   else if(event.type==='trade_opened' || event.type==='trade_closed' && /止损|强平/.test(event.reason || ''))toast(eventText(event));
  }
 }catch{$('events-status').textContent='事件快照读取失败 · 保留已有记录';}
 finally{eventsBusy=false;}
}

// OKX demo-trading account (tools/okx_demo_trader.py) published on the paper-data branch.
async function refreshSim(){
 if(simBusy)return;simBusy=true;
 try{
  const runtime=await runtimeConfig();if(runtime.mode==='unavailable')throw new Error('运行环境配置无法读取');
  const source=new URL(runtime.account_url||OKX_URL);source.searchParams.set('t',Math.floor(Date.now()/60000));
  const response=await fetch(source,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(12000)});
  if(!response.ok)throw new Error();const data=await response.json();if(!validOkx(data))throw new Error();
  const changes=okxChanges(simData,data),fresh=okxNewActions(simData,data);
  const lastUpl=new Map(simData?okxLive(simData,okxMarks).positions.map(p=>[p.inst,p.upl]):[]);
  simData=data;$('sim-error').hidden=true;setError('sim-account',null);syncOkxMarks();pollOkxMarks();renderSim();updateCompanion();updateStreamStatus();
  for(const line of changes)notify(line,`OKX 模拟 · ${line.text}`);
  // One ticket per new exchange action, played one after another.
  fresh.map(a=>tradeBurst(a,lastUpl)).filter(Boolean).forEach((b,i)=>setTimeout(()=>mascot.burst(b),i*3400));
  updateCompanionWork();
 }catch{setError('sim-account','账户快照暂时读取失败，保留上次数据');if(route==='home'){$('sim-error').hidden=false;$('sim-error').textContent='OKX 模拟账户数据暂时读取失败，保留已有数据，稍后自动重试。';}}
 finally{simBusy=false;}
}
// The OKX demo account takes over the home page while the original bot is paused, stopped or stale;
// once the bot publishes fresh snapshots again, the original home page comes back on its own.
// Demo-market mark prices for the OKX demo positions (demo prices differ from the live exchange).
function syncOkxMarks(){
  const symbols=document.hidden?[]:[...new Set((simData?.positions || []).map(p=>p.inst))].sort(),key=symbols.join(',');
  if(key===okxMarkKey)return;
  okxMarkStream?.stop();okxMarkStream=null;okxMarkKey=key;
  if(!symbols.length)return;
  okxMarkStream=new PublicStream({url:OKX_DEMO_PUBLIC_WS,fallbackUrls:[OKX_DEMO_FALLBACK_WS],connectTimeout:8000,args:symbols.map(instId=>({channel:'mark-price',instId})),maxFailures:config.wsMaxFailures,onState:(state,reason)=>{diagNote(`模拟盘标记价 WS：${state}${reason?' · '+reason:''}`);updateStreamStatus();},onData:body=>{
    const id=body.arg?.instId,row=body.data[0],value=number(row?.markPx);
    if(!symbols.includes(id) || !(value>0))return false;
    okxMarks.set(id,{price:value,at:Date.now()});
    // Demo marks arrive ~10× a second; redraw at most once a second so the page reads as calm.
    if(!okxMarkFrame)okxMarkFrame=setTimeout(()=>requestAnimationFrame(()=>{okxMarkFrame=0;if(['home','sim'].includes(route))renderSim();else renderPinnedBalance();}),1000);
  }});
  okxMarkStream.start();
}
// REST fallback for demo mark prices when the demo WebSocket is not delivering (some Safari setups block it):
// one request returns every demo swap's mark price; OKX allows the demo header from this origin (CORS).
let okxMarkPolling=false,okxMarkPollAt=0;
async function pollOkxMarks(){
  const symbols=[...new Set((simData?.positions || []).map(p=>p.inst))];
  const wsLive=okxMarkStream?.state==='实时' && Date.now()-okxMarkStream.lastData<20000;
  if(!symbols.length || wsLive || okxMarkPolling || document.hidden)return;
  okxMarkPolling=true;
  try{
    const response=await fetch('https://www.okx.com/api/v5/public/mark-price?instType=SWAP',{headers:{'x-simulated-trading':'1'},cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(10000)});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const body=await response.json();if(body.code!=='0' || !Array.isArray(body.data))throw new Error(`code ${body.code}`);
    const at=Date.now();
    for(const row of body.data)if(symbols.includes(row.instId) && number(row.markPx)>0)okxMarks.set(row.instId,{price:number(row.markPx),at});
    if(!symbols.every(id=>okxMarks.get(id)?.at===at))throw new Error('响应缺少当前持仓的模拟标记价');
    if(!okxMarkPollAt)diagNote('模拟盘标记价 REST：成功');okxMarkPollAt=at;
    if(['home','sim'].includes(route))renderSim();else renderPinnedBalance();updateStreamStatus();
  }catch(e){diagNote(`模拟盘标记价 REST：失败 ${e.name} ${e.message}`);okxMarkPollAt=0;updateStreamStatus();}
  finally{okxMarkPolling=false;}
}
// Floating PnL moves the equity by a few cents on every 8-second poll; only animate meaningful moves.
const EQUITY_ROLL_STEP=.1;
function simPrimary(){
  // Home is always the paper-data OKX / evolve feeds. The retired crypto-bot snapshots
  // (paper/*.json, paused since 2026-11-02) stay behind “显示原主力盘”.
  if(showLegacy)return false;
  return true;
}
let showLegacy=false;
$('legacy-toggle').addEventListener('click',()=>{showLegacy=!showLegacy;renderSim();});
const statusPill=(el,d)=>{const st=okxStatus(d),cls=`pill okx-pill ${st.cls}`;if(el.textContent!==st.label)el.textContent=st.label;if(el.className!==cls)el.className=cls;};
// Prices tick several times a second. When the same positions are listed, patch only the parts whose text
// changed (PnL, details) so icons and rows stay put instead of the whole list flashing; rebuild on open / close.
function renderOkxPositions(id,openByDefault){
 const owners=Object.fromEntries((evolveData?.paper?.positions||[]).map(x=>[x.inst,{champion:x.champion,boost:x.boost}]));   // which strategy holds each coin on paper
 const box=$(id),html=okxPositionsMarkup(simData,okxMarks,Date.now(),{open:openByDefault,owners});
 const tpl=document.createElement('template');tpl.innerHTML=html;
 const next=[...tpl.content.querySelectorAll('details.okx-pos')],cur=[...box.querySelectorAll('details.okx-pos')];
 if(next.length && next.length===cur.length && next.every((n,i)=>n.dataset.inst===cur[i].dataset.inst && n.className===cur[i].className)){
  next.forEach((n,i)=>{for(const sel of ['.okx-side','.okx-pos-tag','.okx-pos-pnl','.okx-pos-body']){
   const was=cur[i].querySelector(sel),now=n.querySelector(sel);if(was && now && was.outerHTML!==now.outerHTML)was.replaceWith(now);
  }});
  return;
 }
 const state=new Map(cur.map(el=>[el.dataset.inst,el.open]));
 box.replaceChildren(tpl.content);
 box.querySelectorAll('details.okx-pos').forEach(el=>{if(state.has(el.dataset.inst))el.open=state.get(el.dataset.inst);});
}
function retLine(t){
 return `累计收益 <span class="${tone(t.ret)}">${signed(t.ret)}%</span> · 今日 <span class="${tone(t.day)}">${Number.isFinite(t.day)?`${signed(t.day)} USDT（${signed(t.dayPct)}%）`:'—'}</span> · 持仓 ${t.positions} · 已平仓 ${t.trades} 笔`+(o=>o?`<br><small class="okx-outlier">${okxOutlierNote(o)}</small>`:'')(okxOutlier(simData,t));
}
// One-line digest of the account notices; the full notices sit behind it (tap to expand).
const PROFILE_SHORT={aggressive:'激进档',volume_test:'增量测试档',standard:'标准档',evolved:'进化冠军'};
function notesSummary(d,quote,now=Date.now()){
 if(!d)return {text:'账户快照读取中…',warn:false};
 const age=Math.max(0,now-Date.parse(d.updated_at)),mins=Math.floor(age/60000),late=age>OKX_STALE*5/6;   // runs are 4 hours apart: late after 5
 const p=d.execution_profile,pc=x=>(x*100).toFixed(0),risk=!Number.isFinite(p?.risk)?'':p.risk_max>p.risk?` 每笔 ${pc(p.risk)}–${pc(p.risk_max)}%${Number.isFinite(p.leader_risk)?`，第一名 ${pc(p.leader_risk)}%`:''}${p.major_factor>1?`，主流币 ×${p.major_factor}`:''}`:` ${pc(p.risk)}%`;
 const parts=[`快照 ${mins<1?'刚刚':mins<90?mins+' 分钟前':Math.round(mins/60)+' 小时前'}${late?'（延迟）':''}`,quote.live?'行情实时':quote.polling?'行情轮询':quote.needsQuotes?'行情未连接':null,p?`${PROFILE_SHORT[p.mode]||'策略档'}${risk}`:null,d.paused?'已暂停':null].filter(Boolean);
 return {text:parts.join(' · '),warn:late||Boolean(quote.needsQuotes&&!quote.live&&!quote.polling)};
}
// ---- 策略进化 (tools/evolve → paper-data/evolve/dashboard.json) ----
let evolveData=null,evolveBusy=false,evolveSel=null;
async function refreshEvolve(){
 if(evolveBusy || !['home','evolve','sim'].includes(route))return;evolveBusy=true;
 try{
  const r=await fetch(`${EVOLVE_URL}?t=${Math.floor(Date.now()/60000)}`,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(15000)});
  if(!r.ok)throw new Error('读取失败');const d=await r.json();if(!validEvolve(d))throw new Error('格式不对');
  evolveData=d;$('evolve-error').hidden=true;setError('evolve',null);renderEvolve();if(route==='sim'||route==='home')renderSim();
 }catch(e){const msg='进化数据暂时读取失败，稍后自动重试。';setError('evolve',msg);if(!evolveData){const intro=$('evolve-intro');if(intro)intro.innerHTML=`<p class="alert" role="alert">${msg}</p>`;if(route==='evolve'){$('evolve-error').hidden=false;$('evolve-error').textContent=msg;}}}
 finally{evolveBusy=false;}
}
// Per-node genes and curves (nodes.json), fetched only when a node without them is opened; once per generation.
const evolveDetails={data:null,tried:null,busy:false,failed:false};
async function loadEvolveDetails(){
 const gen=evolveData?.generation;if(evolveDetails.busy || evolveDetails.tried===gen)return;
 evolveDetails.busy=true;evolveDetails.tried=gen;
 try{const r=await fetch(`${EVOLVE_NODES_URL}?t=${Math.floor(Date.now()/60000)}`,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(20000)});
  const x=r.ok?await r.json():null;if(!validNodeDetails(x))throw new Error('bad');evolveDetails.data=x;evolveDetails.failed=false;}
 catch{evolveDetails.failed=true;setTimeout(()=>{if(evolveDetails.tried===gen)evolveDetails.tried=null;renderEvolve();},60000);}
 finally{evolveDetails.busy=false;renderEvolve();}
}
function renderEvolve(){
 const d=evolveData;
 $('home-evolve').hidden=!d;if(d)setHTML($('home-evolve'),homeCardMarkup(d));
 if(route!=='evolve' || !d)return;
 if(!evolveSel || !d.nodes.some(n=>n.id===evolveSel))evolveSel=d.champion?.id || d.nodes[0]?.id || null;
 {const u=updateSchedule(d.updated_at);setText($('evolve-updated'),`第 ${d.generation} 代 · ${u.line}`);$('evolve-updated').classList.toggle('stale',u.stale);const intro=$('evolve-intro');if(intro)intro.innerHTML=evolveIntroMarkup(d);}
 setHTML($('evolve-loop'),loopSVG(d));setHTML($('evolve-stats'),statsMarkup(d));setHTML($('evolve-fitness'),fitnessSVG(d.history));
 setHTML($('evolve-graph'),graphSVG(d,evolveSel));geneFx.refresh();setHTML($('evolve-detail'),detailMarkup(d,evolveSel,evolveDetails.data,evolveDetails.failed));
 if(needsDetails(d,evolveSel,evolveDetails.data))loadEvolveDetails();
 // On narrow screens the gene pool scrolls sideways: start at the newest generation unless the viewer has scrolled.
 {const box=$('evolve-graph');if(!box.dataset.userScrolled && box.scrollWidth>box.clientWidth){box.dataset.auto='1';box.scrollLeft=box.scrollWidth;}}
 setHTML($('evolve-paper'),paperMarkup(d));setText($('evolve-caption'),dataCaption(d));
 // Replay: the whole history on first view, only the new generations when fresh data arrives.
 const gens=replayGenerations(d);
 if(evoReplay.timer)evoApply(gens[evoReplay.i-1] ?? gens[0]);
 else if(evoReplay.seen===null){evoReplay.seen=d.generation;if(!evoStill())evoPlay(gens[0]);else evoShowAll();}
 else if(d.generation>evoReplay.seen){const from=evoReplay.seen+1;evoReplay.seen=d.generation;if(!evoStill())evoPlay(from);}
 else if(!evoReplay.timer)evoShowAll();
}
// ---- gene pool replay (generation by generation) ----
const evoReplay={timer:0,gens:[],i:0,seen:null};
const evoStill=()=>matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.classList.contains('reduced-motion');
const geneFx=new GeneFx($('evolve-graph'),{still:evoStill,active:()=>route==='evolve'});
function evoApply(g){
 const svg=$('evolve-graph').querySelector('svg');if(!svg || g===undefined)return;
 svg.classList.add('replay');
 svg.querySelectorAll('[data-born]').forEach(el=>{const st=replayState(el.dataset,g);el.classList.toggle('future',st.future);el.classList.toggle('gone',st.gone);el.classList.toggle('newborn',st.newborn);});
 const label=svg.querySelector('.evo-gen-label');if(label)label.textContent=`第 ${g} 代`;
}
function evoShowAll(){
 const svg=$('evolve-graph').querySelector('svg');if(svg){svg.classList.remove('replay');svg.querySelectorAll('.future,.gone,.newborn').forEach(el=>el.classList.remove('future','gone','newborn'));}
}
function evoStop(showAll=true){
 clearTimeout(evoReplay.timer);evoReplay.timer=0;if(showAll)evoShowAll();
}
function evoPlay(fromGen){
 if(!evolveData)return;
 clearTimeout(evoReplay.timer);
 evoReplay.gens=replayGenerations(evolveData);
 evoReplay.i=Math.max(0,evoReplay.gens.findIndex(g=>g>=fromGen));
 const delays=replayDelays(evolveData,evoReplay.gens,fromGen>evoReplay.gens[0]?5000:12000);   // full history ≈ 12 s, new generations ≈ 5 s
 const tick=()=>{if(route!=='evolve' || document.hidden){evoStop();return;}
  if(evoReplay.i>=evoReplay.gens.length){evoStop();return;}
  const k=evoReplay.i;evoApply(evoReplay.gens[k]);evoReplay.i++;evoReplay.timer=setTimeout(tick,delays[k]);};
 evoReplay.timer=1;tick();
}
function selectEvolveNode(id,focus=false){
 if(!evolveData || !id)return;evolveSel=id;renderEvolve();
 if(focus)$('evolve-detail').scrollIntoView({behavior:'smooth',block:'nearest'});
}
$('evolve-graph').addEventListener('scroll',e=>{const box=e.currentTarget;if(box.dataset.auto){delete box.dataset.auto;return;}box.dataset.userScrolled='1';},{passive:true});
$('evolve-page').addEventListener('click',e=>{const n=e.target.closest('[data-node]');if(n && evoReplay.timer)evoStop();if(n)selectEvolveNode(n.dataset.node,n.closest('#evolve-detail')===null && innerWidth<900);});
$('evolve-page').addEventListener('keydown',e=>{const n=e.target.closest('[data-node]');if(n && (e.key==='Enter' || e.key===' ')){e.preventDefault();selectEvolveNode(n.dataset.node,true);}});

// Write only when content changed: identical rewrites still rebuild nodes (SVG, lists) and read as flicker.
const setHTML=(el,html)=>{if(el && el.__html!==html){el.innerHTML=html;el.__html=html;}};
const setText=(el,text)=>{if(el && el.textContent!==text)el.textContent=text;};
function renderSim(){
 renderTradeControls(simData);updateCompanionWork();
 const quote=demoQuoteState(simData,okxMarks,okxMarkStream,okxMarkPollAt);
 document.querySelectorAll('[data-account-note]').forEach(el=>setText(el,accountDataNote(simData,quote)));
 {const n=notesSummary(simData,quote);document.querySelectorAll('[data-notes-summary]').forEach(el=>{setText(el,n.text);el.closest('.okx-notes')?.classList.toggle('warn',n.warn);});}
 const primary=simPrimary();
 document.documentElement.classList.toggle('sim-primary',primary);document.documentElement.classList.toggle('show-legacy',primary && showLegacy);
 $('home-sim').hidden=!primary;
 setText($('legacy-toggle'),showLegacy?'收起原主力盘':'显示原主力盘（crypto-bot · 已暂停）');$('legacy-toggle').setAttribute('aria-expanded',String(showLegacy));
 if(primary && route==='home'){
  if(!simData){renderPinnedBalance();return;}
  const t=okxTotals(simData,okxMarks);
  rollNumber($('okx-total-equity'),t.equity,EQUITY_ROLL_STEP);
  setHTML($('okx-total-ret'),retLine(t));
  setText($('okx-total-updated'),`更新 ${time(simData.updated_at)} UTC+7`);statusPill($('home-okx-status'),simData);
  setHTML($('home-okx-curve'),okxCurveSVG(simData));
  renderOkxPositions('home-okx-positions',false);setText($('home-okx-pos-count'),String(simData.positions.length));
  setHTML($('home-okx-actions'),okxActionsMarkup(simData,15,{kind:'trades'}));setHTML($('home-okx-log'),okxActionsMarkup(simData,30,{kind:'system'}));
  setHTML($('okx-compare'),mirrorCompareMarkup(simData,evolveData));
  setHTML($('okx-daily'),okxDailyMarkup(okxDailyReturns(simData,t.equity),10));
 }
 renderPinnedBalance();
}
document.addEventListener('visibilitychange',()=>{
 if(document.hidden){quoteStream?.stop();candleStream?.stop();quoteStreamKey='';candleStreamKey='';syncOkxMarks();}
 else {syncStreams();refreshPrices();refreshPublicAccount();refreshTeam();refreshScan();refreshEvents();refreshSim();if(['home','coin'].includes(route))refreshCandles(true);}
});
async function refreshDecisionHistory(){
 if(decisionHistoryBusy)return;decisionHistoryBusy=true;
 try{const r=await fetch('./paper/history.json',{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(10000)});if(!r.ok)throw new Error();const d=await r.json();if(d.schema_version!==1||!Array.isArray(d.alert_groups))throw new Error();decisionHistory=d;decisionHistoryStatus='ready';}
 catch{decisionHistoryStatus='error';}finally{decisionHistoryBusy=false;updateCompanion();}
}
document.addEventListener('modi-decisions-open',refreshDecisionHistory);

initTradeControls();refreshRuntimeHealth();navigate();
(function initTheme(){
  const btn=$('theme-toggle'); if(!btn)return;
  const effective=()=>document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
  const paint=()=>{const now=effective();btn.textContent=now==='dark'?'亮':'暗';btn.setAttribute('aria-pressed',String(now==='dark'));btn.title=now==='dark'?'切换到浅色':'切换到深色';};
  paint();
  btn.addEventListener('click',()=>{const next=effective()==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',next);try{localStorage.setItem('modi-theme',next);}catch{}paint();});
})();
refreshPrices();refreshEvents();
setInterval(()=>{if(!document.hidden){refreshPrices();pollOkxMarks();renderPinnedBalance();updateStreamStatus();updateCompanionWork();if(route==='home')renderHome();}},8000);
setInterval(()=>{if(!document.hidden){
 if(['home','coin'].includes(route))refreshCandles();
 refreshSnapshot();refreshPublicAccount();refreshTeam();refreshScan();refreshEvents();refreshSlow();refreshSim();refreshEvolve();
}},60000);
setInterval(()=>{if(!document.hidden){refreshHealth();if(route==='home')renderHome();
 for(const stream of [quoteStream,candleStream])if(stream?.state==='实时' && Date.now()-stream.lastData>45000)stream.socket?.close();
 refreshRuntimeHealth();}},10000);
const updateClock = () => { $('utc-clock').textContent = new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Bangkok',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(Date.now()) + ' UTC+7'; $('day-number').textContent = 'DAY ' + dayNumber(config.startDate); };
updateClock(); setInterval(updateClock, 1000);

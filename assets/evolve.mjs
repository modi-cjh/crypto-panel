// 策略进化 page: renders paper-data/evolve/dashboard.json written by tools/evolve/run.py. Pure functions (unit-tested).
export const EVOLVE_URL = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/evolve/dashboard.json';
// Schema 2 keeps lineage nodes slim; genes, summary and curves of every node are in nodes.json, loaded when a node is opened.
export const EVOLVE_NODES_URL = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/evolve/nodes.json';
export const SPECIES_COLORS = { momentum: '#2f6fb0', mean_revert: '#7b4bd1', vol_breakout: '#d98200', trend: '#1f9d63' };
const SPECIES_ORDER = ['momentum', 'mean_revert', 'vol_breakout', 'trend'];
const finite = Number.isFinite;
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmt = (v, d = 2) => finite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—';
const signed = (v, d = 2) => finite(v) ? (v > 0 ? '+' : '') + fmt(v, d) : '—';
const tone = v => finite(v) ? v >= 0 ? 'up' : 'down' : '';
const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
const day = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', year: 'numeric', month: '2-digit', day: '2-digit' });
const time = t => finite(typeof t === 'number' ? t : Date.parse(t)) ? clock.format(typeof t === 'number' ? t : Date.parse(t)) : '—';
const date = t => finite(Date.parse(t)) ? day.format(Date.parse(t)) : '—';
const coin = inst => String(inst).split('-')[0];
const GENE_NAMES = { species: '物种', direction: '方向', lookback: '回看窗口', threshold: '偏离阈值 σ', expand: '振幅倍数', fast: '快线 EMA', slow: '慢线 EMA',
  stop_atr: '止损 ×ATR', target_rr: '止盈 R', max_hold: '最长持有 h', risk: '单笔风险', vol_min: '波动下限', vol_max: '波动上限' };
const USED = { momentum: ['lookback'], mean_revert: ['lookback', 'threshold'], vol_breakout: ['expand'], trend: ['fast', 'slow'] };
const HOW = { seed: '初始随机', immigrant: '随机移民', crossover: '交叉', mutation: '突变', niche: '物种保护' };
const REASON = { stop: '止损', target: '止盈', time: '到期', switch: '换冠军平仓' };
const SWITCH_WHY = { first: '补入空位', challenger: '挑战者接替', breakdown: '原成员样本外失守', carry: '沿用原冠军' };
// Team members (the paper-trading champions); older dashboards only have a single champion.
export const teamIds = d => (d.team && d.team.length ? d.team.map(m => m.id) : d.champion ? [d.champion.id] : []);

export function validEvolve(d) {
  return Boolean(d) && typeof d === 'object' && (d.schema === 1 || d.schema === 2) && finite(d.generation) && Array.isArray(d.nodes) && Array.isArray(d.history) &&
    d.nodes.every(n => n && typeof n.id === 'string' && SPECIES_ORDER.includes(n.species) && (n.genes || d.schema === 2) && n.train && n.test);
}
export function validNodeDetails(x) {
  return Boolean(x) && typeof x === 'object' && x.schema === 1 && finite(x.generation) && x.nodes && typeof x.nodes === 'object' &&
    Object.values(x.nodes).every(v => v && typeof v.genes === 'object' && v.genes);
}
// A node with its details merged in (slim lineage node + nodes.json entry); seated members already carry everything.
// A node is slim when it lacks genes (schema 2) or, with d.details set, lacks its summary (genes kept for older pages).
const isSlim = (d, n) => !n.genes || (Boolean(d.details) && n.summary === undefined);
export function fullNode(d, id, details) {
  const n = d.nodes.find(x => x.id === id) || (d.champion?.id === id ? d.champion : null);
  if (!n || !isSlim(d, n)) return n;
  const extra = details?.nodes?.[id];
  return extra ? { ...extra, ...n } : n;
}
export const needsDetails = (d, id, details) => { const n = fullNode(d, id, details); return Boolean(n && isSlim(d, n)); };
export const speciesName = (d, sp) => d?.species_names?.[sp] || sp;

// ---- evolution loop: observe → breed → test → out-of-sample gate → select → deploy
const PHASES = [['观察波动', '读取市场波动率，决定变异强度'], ['交叉 · 突变', '锦标赛选择父母，交叉与突变产生后代'], ['训练集回测', '前 70% 数据计算适应度'],
  ['样本外检验', '后 30% 没见过的数据：收益、回撤、交易次数'], ['选择淘汰', '精英保留，其余被新一代取代'], ['部署冠军', '合格者中训练适应度最高的接管纸面交易']];
export function loopSVG(d) {
  const cx = 160, cy = 160, r = 112;
  const pts = PHASES.map((_, i) => { const a = -Math.PI / 2 + i * 2 * Math.PI / PHASES.length; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; });
  const v = d.volatility || {};
  return `<svg class="evo-loop" viewBox="0 0 320 320" role="img" aria-label="进化循环：${PHASES.map(p => p[0]).join('、')}">` +
    `<circle cx="${cx}" cy="${cy}" r="${r}" class="evo-ring"/><circle cx="${cx}" cy="${cy}" r="${r}" class="evo-ring-run"/>` +
    pts.map(([x, y], i) => `<g class="evo-phase"><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="9"/><text x="${x.toFixed(1)}" y="${(y + (y > cy ? 25 : -15)).toFixed(1)}" text-anchor="middle">${PHASES[i][0]}</text><title>${PHASES[i][1]}</title></g>`).join('') +
    `<text x="${cx}" y="${cy - 22}" text-anchor="middle" class="evo-loop-cap">第</text><text x="${cx}" y="${cy + 14}" text-anchor="middle" class="evo-loop-gen">${d.generation}</text>` +
    `<text x="${cx}" y="${cy + 38}" text-anchor="middle" class="evo-loop-cap">代 · 波动${esc(v.label || '—')}</text></svg>`;
}

export function statsMarkup(d) {
  const h = d.history, last = h.at(-1), ref = h.length > 10 ? h.at(-11) : h[0];
  const delta = last && ref && ref.best ? (last.best - ref.best) / Math.abs(ref.best) * 100 : null;
  const v = d.volatility || {};
  return `<div class="history-metrics evo-stats">${[
    ['种群', `${d.stats.population}`], ['每代新生', `${d.stats.offspring}`], ['精英保留', `${d.stats.elites}`],
    ['累计出生', `${fmt(d.stats.born, 0)}`], ['累计淘汰', `${fmt(d.stats.killed, 0)}`], ['本代样本外合格', `${last ? last.qualified : '—'} / ${d.stats.population}`],
    ['最佳适应度', last ? fmt(last.best, 3) : '—'], ['近 10 代适应度变化', delta === null ? '—' : `${signed(delta, 1)}%`, tone(delta)],
    ['市场波动', `${esc(v.label || '—')} · ${fmt(v.market, 2)}×`], ['变异率', finite(v.mutation_rate) ? `${Math.round(v.mutation_rate * 100)}%` : '—'],
    ['物种分布', last?.species ? SPECIES_ORDER.map(sp => `${esc(speciesName(d, sp).slice(0, 2))} ${last.species[sp] ?? 0}`).join(' · ') : '—']]
    .map(([k, val, c = '']) => `<div><span>${k}</span><strong class="${c}">${val}</strong></div>`).join('')}</div>`;
}

// ---- fitness by generation: best and population mean
export function fitnessSVG(history) {
  if (!history.length) return '<p class="empty">等待第一代</p>';
  const W = 680, H = 220, L = 54, R = 12, T = 14, B = 30;
  const xs = history.map(r => r.gen), g0 = xs[0], g1 = Math.max(xs.at(-1), g0 + 1);
  const vals = history.flatMap(r => [r.best, r.mean]), lo = Math.min(0, ...vals), hi = Math.max(...vals, lo + .5);
  const x = g => L + (g - g0) / (g1 - g0) * (W - L - R), y = v => T + (hi - v) / (hi - lo) * (H - T - B);
  const line = k => history.map(r => `${x(r.gen).toFixed(1)},${y(r[k]).toFixed(1)}`).join(' ');
  const grid = [hi, (hi + lo) / 2, lo].map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="equity-grid"/><text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${fmt(v, 2)}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" class="evo-fitness" role="img" aria-label="每代最佳与平均适应度">${grid}` +
    `<polyline points="${line('mean')}" class="evo-mean"/><polyline points="${line('best')}" class="evo-best"/>` +
    `<text x="${L}" y="${H - 8}">第 ${g0} 代</text><text x="${W - R}" y="${H - 8}" text-anchor="end">第 ${history.at(-1).gen} 代</text></svg>` +
    `<div class="sim-legend"><span><i style="background:#06727d"></i>最佳</span><span><i style="background:#9aa4b2"></i>种群平均</span></div>`;
}

// ---- gene pool: x = generation born, y = species band (+ stable jitter), edges = parent → child
const hash = s => { let h = 2166136261; for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return (h >>> 0) / 4294967295; };
export function graphLayout(d, W = 1000, H = 520) {
  // x: evenly spaced by the generations that have strategies (an old champion from generation 0 no longer stretches the
  // axis and pushes the recent generations into a thin strip on the right).
  const nodes = d.nodes, cols = [...new Set(nodes.map(n => n.born))].sort((a, b) => a - b);
  const col = Object.fromEntries(cols.map((g, i) => [g, cols.length > 1 ? i / (cols.length - 1) : .5]));
  const band = (H - 60) / SPECIES_ORDER.length, pos = {};
  const fits = nodes.map(n => n.fitness).sort((a, b) => a - b), rank = f => fits.length > 1 ? fits.indexOf(f) / (fits.length - 1) : .5;
  for (const n of nodes) {
    const b = SPECIES_ORDER.indexOf(n.species);
    pos[n.id] = { x: 50 + col[n.born] * (W - 90) + (hash(n.id + 'x') - .5) * Math.min(14, (W - 90) / Math.max(1, cols.length) * .6), y: 40 + b * band + 12 + hash(n.id) * (band - 24), r: 4 + rank(n.fitness) * 8 };
  }
  return pos;
}
export function graphSVG(d, selected) {
  const W = 1000, H = 520, pos = graphLayout(d, W, H), pop = new Set(d.population || []), hall = new Set(d.hall || []), champ = d.champion?.id, team = new Set(teamIds(d));
  const byId = Object.fromEntries(d.nodes.map(n => [n.id, n]));
  const edges = d.nodes.flatMap(n => n.parents.filter(p => pos[p]).map(p => {
    const a = pos[p], b = pos[n.id], hot = selected && (n.id === selected || p === selected);
    // data-born drives the replay; edges into living strategies ('flow') get a travelling spark drawn by evolve-fx.mjs.
    return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" pathLength="100" data-born="${n.born}" class="evo-edge${hot ? ' hot' : ''}${n.how === 'crossover' ? ' cross' : ''}${pop.has(n.id) ? ' flow' : ''}"/>`;
  })).join('');
  const bands = SPECIES_ORDER.map((sp, i) => { const y = 40 + i * (H - 60) / SPECIES_ORDER.length;
    return `<text x="8" y="${(y + 14).toFixed(1)}" class="evo-band" fill="${SPECIES_COLORS[sp]}">${esc(speciesName(d, sp))} · ${d.nodes.filter(n => n.species === sp && pop.has(n.id)).length}</text>`; }).join('');
  const order = [...d.nodes].sort((a, b) => (pop.has(a.id) - pop.has(b.id)) || a.fitness - b.fitness);
  const dots = order.map(n => {
    const p = pos[n.id], cls = ['evo-node', pop.has(n.id) ? 'alive' : 'dead', n.qualified ? 'ok' : '', hall.has(n.id) ? 'hall' : '', n.id === champ ? 'champ' : '', team.has(n.id) ? 'team' : '', n.id === selected ? 'sel' : ''].filter(Boolean).join(' ');
    const label = `${speciesName(d, n.species)} ${n.id} · 第 ${n.born} 代 · 适应度 ${fmt(n.fitness, 2)} · 样本外 ${signed(n.test.ret, 1)}%${n.qualified ? ' · 合格' : ''}${pop.has(n.id) ? '' : ` · 第 ${n.died ?? '—'} 代淘汰`}`;
    return `<g class="${cls}" data-node="${esc(n.id)}" data-born="${n.born}"${n.died == null ? '' : ` data-died="${n.died}"`} tabindex="0" role="button" aria-label="${esc(label)}"><circle class="hit" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${(p.r + 9).toFixed(1)}"/>` +
      `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${p.r.toFixed(1)}" fill="${SPECIES_COLORS[n.species]}"/>${team.has(n.id) ? `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${(p.r + 5).toFixed(1)}" class="${n.id === champ ? 'champ-ring' : 'team-ring'}"/>` : ''}<title>${esc(label)}</title></g>`;
  }).join('');
  const g0 = Math.min(...d.nodes.map(n => n.born)), g1 = Math.max(...d.nodes.map(n => n.born));
  return `<div class="evo-stage"><svg viewBox="0 0 ${W} ${H}" class="evo-graph" role="group" aria-label="基因池：每个点是一个策略，点击查看基因与回测">${bands}<g>${edges}</g><g>${dots}</g>` +
    `<text x="50" y="${H - 6}" class="evo-axis">第 ${g0} 代</text><text x="${W - 40}" y="${H - 6}" text-anchor="end" class="evo-axis">第 ${g1} 代 →</text>` +
    `<text x="${W - 16}" y="38" text-anchor="end" class="evo-gen-label" aria-hidden="true"></text></svg><canvas class="evo-fx" aria-hidden="true"></canvas></div>`;
}

// Generations to step through when replaying the gene pool (every generation that has a strategy in the archive).
export function replayGenerations(d) {
  return [...new Set(d.nodes.map(n => n.born))].sort((a, b) => a - b);
}
// Time to spend on each replayed generation: busy generations (many births) get longer, sparse ancestor-only
// generations flash by; the whole replay takes about `total` ms.
export function replayDelays(d, gens, total = 9000) {
  const count = {}; for (const n of d.nodes) count[n.born] = (count[n.born] || 0) + 1;
  const w = gens.map(g => 1 + (count[g] || 0) / 6), sum = w.reduce((a, b) => a + b, 0) || 1;
  return w.map(x => Math.max(180, Math.min(1200, Math.round(x / sum * total))));
}
// Replay state at generation g: which nodes / edges exist yet, which have been eliminated, which were just born.
export function replayState(el, g) {
  const born = Number(el.born), died = el.died === undefined || el.died === null ? null : Number(el.died);
  return { future: born > g, gone: died !== null && died <= g, newborn: born === g };
}

// ---- small equity curve: each series normalised to its own start (100 = break-even), x spread over its own span
export function miniCurve(curves) {
  const series = curves.filter(c => c.pts && c.pts.length > 1 && c.pts[0][1] > 0).map(c => ({ ...c, norm: c.pts.map(p => p[1] / c.pts[0][1] * 100) }));
  if (!series.length) return '<p class="empty">暂无曲线</p>';
  const W = 320, H = 110, all = series.flatMap(c => c.norm);
  let lo = Math.min(...all, 100), hi = Math.max(...all, 100);
  if (hi - lo < 1) { lo -= .5; hi += .5; }
  const y = v => (6 + (hi - v) / (hi - lo) * (H - 12)).toFixed(1);
  const paths = series.map(c => `<polyline points="${c.norm.map((v, i) => `${(6 + i / (c.norm.length - 1) * (W - 12)).toFixed(1)},${y(v)}`).join(' ')}" class="${c.cls}"/>`);
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="evo-mini" role="img" aria-label="${series.map(c => `${c.label} ${signed(c.norm.at(-1) - 100, 1)}%`).join('、')}">` +
    `<line x1="6" x2="${W - 6}" y1="${y(100)}" y2="${y(100)}" class="sim-base"/>${paths.join('')}</svg>`;
}

const metricRow = (label, m) => `<tr><th>${label}</th><td class="${tone(m.ret)}">${signed(m.ret, 2)}%</td><td>${fmt(m.max_dd, 2)}%</td><td>${m.trades}</td><td>${fmt(m.win_rate, 1)}%</td><td>${m.profit_factor == null ? '—' : fmt(m.profit_factor, 2)}</td></tr>`;

export function detailMarkup(d, id, details = null, detailsFailed = false) {
  const n = fullNode(d, id, details);
  if (!n) return '<p class="empty">点击基因池里的任意节点，查看它的基因与回测</p>';
  const pop = new Set(d.population || []), champ = d.champion?.id === n.id, seat = teamIds(d).indexOf(n.id);
  const status = champ ? '<span class="pill okx-pill up">冠军组合领头 · 纸面交易中</span>' : seat >= 0 ? '<span class="pill okx-pill up">冠军组合成员 · 纸面交易中</span>' : pop.has(n.id) ? '<span class="pill">存活</span>' : `<span class="pill muted">第 ${n.died ?? '—'} 代淘汰</span>`;
  const gate = n.qualified ? '<span class="pill okx-pill up">样本外合格</span>' : '<span class="pill okx-pill down">样本外不合格</span>';
  const used = new Set(['species', 'direction', 'stop_atr', 'target_rr', 'max_hold', 'risk', 'vol_min', 'vol_max', ...(USED[n.species] || [])]);
  const bounds = d.gene_bounds || {};
  const genes = Object.keys(GENE_NAMES).filter(k => n.genes && k in n.genes).map(k => {
    const v = n.genes[k], b = bounds[k];
    const shown = k === 'species' ? speciesName(d, v) : k === 'direction' ? { both: '多空', long: '只做多', short: '只做空' }[v] : k === 'risk' ? `${fmt(v * 100, 2)}%` : k === 'target_rr' && !v ? '无' : esc(v);
    const bar = Array.isArray(b) && b.length === 2 && finite(b[0]) ? `<i class="evo-gbar"><b style="width:${Math.max(2, Math.min(100, (v - b[0]) / (b[1] - b[0]) * 100)).toFixed(0)}%"></b></i>` : '';
    return `<tr class="${used.has(k) ? '' : 'inert'}"><th>${GENE_NAMES[k]}</th><td>${shown}</td><td>${bar}</td></tr>`;
  }).join('');
  const parents = n.parents.length ? n.parents.map(p => d.nodes.some(x => x.id === p) ? `<button type="button" class="pill" data-node="${esc(p)}">${esc(p)}</button>` : `<span class="pill muted">${esc(p)}</span>`).join(' ') : '无（随机生成）';
  const curves = [{ pts: n.train_curve, cls: 'evo-train', label: '训练集' }, { pts: n.test_curve, cls: 'evo-test', label: '样本外' }].filter(c => c.pts && c.pts.length);
  return `<div class="evo-detail-head"><h3><i class="evo-dot" style="background:${SPECIES_COLORS[n.species]}"></i>${esc(speciesName(d, n.species))} · ${esc(n.id)}</h3>${status}${gate}</div>` +
    `<p class="evo-summary">${esc(n.summary || '')}</p>` +
    `<p class="muted evo-origin">第 ${n.born} 代出生 · ${HOW[n.how] || esc(n.how)} · 父代 ${parents} · 训练适应度 <b>${fmt(n.fitness, 3)}</b></p>` +
    `<div class="table-scroll"><table class="evo-metrics"><thead><tr><th></th><th>收益</th><th>最大回撤</th><th>交易次数</th><th>胜率</th><th>盈亏比</th></tr></thead><tbody>${metricRow('训练集 70%', n.train)}${metricRow('样本外 30%', n.test)}</tbody></table></div>` +
    (!isSlim(d, n) ? `${miniCurve(curves)}<div class="sim-legend">${curves.map(c => `<span><i class="${c.cls}"></i>${c.label}</span>`).join('')}</div>` +
      `<h4>基因</h4><table class="evo-genes"><tbody>${genes}</tbody></table><p class="muted evo-note">灰色基因当前物种用不到，但会遗传给后代。</p>`
      : `<p class="muted evo-loading" role="status">${detailsFailed ? '基因与回测曲线暂时读取失败，稍后自动重试' : '正在读取基因与回测曲线…'}</p>`);
}

export function paperMarkup(d) {
  const p = d.paper, c = d.champion;
  if (!p || !c) return '<p class="empty">还没有样本外合格的策略，纸面交易等待冠军出现</p>';
  const who = x => x.champion ? ` <small class="muted">${esc(x.champion)}</small>` : '';
  const pos = (p.positions || []).map(x => `<li><span><b class="${x.side === 1 ? 'up' : 'down'}">${x.side === 1 ? '多' : '空'}</b> ${esc(coin(x.inst))} @ ${fmt(x.entry, 4)} · 止损 ${fmt(x.stop, 4)}${x.target ? ` · 止盈 ${fmt(x.target, 4)}` : ''}${who(x)}</span><time>${time(x.opened)}</time></li>`).join('');
  const pend = (p.pending || []).map(x => `<li><span><b class="${x.side === 1 ? 'up' : 'down'}">${x.side === 1 ? '多' : '空'}</b> ${esc(coin(x.inst))} · 下一根 K 线开盘成交${who(x)}</span></li>`).join('');
  const trades = (p.trades || []).slice(0, 15).map(t => `<li><span><b class="${t.side === 1 ? 'up' : 'down'}">${t.side === 1 ? '多' : '空'}</b> ${esc(coin(t.inst))} · ${REASON[t.reason] || esc(t.reason)}${who(t)}</span><span class="${tone(t.pnl)}">${signed(t.pnl)} U</span><time>${time(t.closed)}</time></li>`).join('');
  const segs = (p.segments || []).slice(0, 8).map(s => `<li><span>${Number.isInteger(s.slot) ? `席位 ${s.slot + 1} · ` : ''}${esc(s.champion)} · ${esc(speciesName(d, s.species))}${SWITCH_WHY[s.reason] ? ` · ${SWITCH_WHY[s.reason]}` : ''}</span><span>${time(s.from_ts)} 起${s.to_ts ? ` · ${time(s.to_ts)} 止` : ' · 当前'}</span></li>`).join('');
  const r = d.champion_rules, now = Date.parse(d.updated_at);
  const byId = Object.fromEntries((d.team || []).map(t => [t.id, t]));
  const members = (p.members || []).map(m => { const t = byId[m.id], held = finite(Date.parse(m.since)) ? (now - Date.parse(m.since)) / 3600000 : null;
    return `<tr data-node="${esc(m.id)}" class="evo-member"><td>${m.rank === 1 ? '领头' : `#${m.rank}`}</td><td><i class="evo-dot" style="background:${SPECIES_COLORS[m.species]}"></i>${esc(m.id)} · ${esc(speciesName(d, m.species))}</td>` +
      `<td>${t ? `${signed(t.test.ret, 1)}% / ${t.test.trades} 笔` : '—'}</td><td>${fmt(m.equity)}</td><td class="${tone(m.ret)}">${signed(m.ret, 2)}%</td><td>${m.positions}</td><td>${held === null ? '—' : fmt(held, 1) + 'h'}</td></tr>`; }).join('');
  const rules = r ? `<p class="muted evo-rules">样本外合格的策略中训练适应度最高的 ${r.team_size || 5} 个组成冠军组合（同一物种最多 ${r.species_cap || 2} 个），各分 1/${r.team_size || 5} 资金独立交易。每个成员最短任期 ${r.min_tenure_hours} 小时；期满后，连续 ${r.streak} 次样本外合格、训练适应度高出最弱成员 ${Math.round((r.margin - 1) * 100)}% 的挑战者替换它（每次运行最多换一个）；成员样本外亏损超过 ${-r.retain.min_ret}% 或回撤超过 ${r.retain.max_dd}% 时立即换下。OKX 模拟盘同一币种只跟排名靠前的成员。</p>` : '';
  const chal = (d.challengers || []).map(x => `<li><span>${esc(x.id)} · ${esc(speciesName(d, x.species))} · 适应度 ${fmt(x.fitness, 2)}</span><span>连续合格 <b>${x.streak}</b> / ${r ? r.streak : 3}</span></li>`).join('');
  return `<div class="history-metrics evo-stats">${[['纸面权益 USDT', fmt(p.equity)], ['收益', `${signed(p.ret, 2)}%`, tone(p.ret)], ['组合成员', `${(p.members || []).length}`], ['持仓', `${(p.positions || []).length}`], ['已平仓', `${(p.trades || []).length}`]]
      .map(([k, v, cl = '']) => `<div><span>${k}</span><strong class="${cl}">${v}</strong></div>`).join('')}</div>` +
    `${miniCurve([{ pts: p.curve, cls: 'evo-test', label: '纸面' }])}` +
    (members ? `<h4>冠军组合</h4><div class="table-scroll"><table class="evo-metrics evo-team"><thead><tr><th></th><th>策略</th><th>样本外</th><th>权益</th><th>收益</th><th>持仓</th><th>任期</th></tr></thead><tbody>${members}</tbody></table></div>` :
      `<h4>当前冠军 · ${esc(c.id)}</h4><p class="evo-summary">${esc(c.summary)}</p>`) + rules +
    `<h4>持仓</h4><ul class="evo-list">${pos + pend || '<li class="empty">空仓，等待组合成员的信号</li>'}</ul>` +
    `<h4>纸面成交</h4><ul class="evo-list">${trades || '<li class="empty">还没有平仓</li>'}</ul>` +
    `<h4>席位变动</h4><ul class="evo-list">${segs}</ul>` +
    (chal ? `<h4>挑战者</h4><ul class="evo-list">${chal}</ul>` : '');
}

export function homeCardMarkup(d) {
  const c = d.champion, last = d.history.at(-1);
  const n = teamIds(d).length;
  return `<span class="evo-home-gen">第 ${d.generation} 代</span><span class="evo-home-main">${c ? `冠军组合 ${n} 个 · 领头 ${esc(speciesName(d, c.species))} · 样本外 <b class="${tone(c.test.ret)}">${signed(c.test.ret, 1)}%</b>` : '等待第一个样本外合格的策略'}</span>` +
    `<span class="muted">本代合格 ${last ? last.qualified : '—'} / ${d.stats.population} · 纸面 ${d.paper ? signed(d.paper.ret, 2) + '%' : '—'} · 查看进化 →</span>`;
}

export function dataCaption(d) {
  const x = d.data, g = d.gate;
  return `数据：${x.instruments.map(coin).join(' / ')} · 1 小时 K 线 · 共 ${fmt(x.bars, 0)} 根。训练集 ${date(x.train_from)} – ${date(x.train_to)}（70%），样本外 ${date(x.test_from)} – ${date(x.test_to)}（30%，策略进化时从没见过）。` +
    `样本外合格条件：收益 > ${g.min_ret}%、最大回撤 < ${g.max_dd}%、交易 ≥ ${g.min_trades} 次。回测与纸面交易共用同一套执行逻辑（下一根开盘成交、手续费 0.05%、滑点 0.02%、同根 K 线同时触及止损与止盈按止损计）。更新 ${time(d.updated_at)} UTC+7`;
}

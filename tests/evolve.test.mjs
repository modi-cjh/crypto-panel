import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { replayGenerations, replayState, replayDelays, validEvolve, loopSVG, statsMarkup, fitnessSVG, graphLayout, graphSVG, detailMarkup, paperMarkup, homeCardMarkup, dataCaption, miniCurve, updateSchedule, nextPaperUpdate, evolveIntroMarkup, entryGateMarkup, entryGateView, currentAccountTrades, PAPER_STALE_MS } from '../assets/evolve.mjs';

const d = JSON.parse(fs.readFileSync(new URL('./fixtures/evolve-dashboard.json', import.meta.url), 'utf8'));

test('dashboard from the engine is accepted; broken ones are rejected', () => {
  assert.ok(validEvolve(d));
  assert.equal(validEvolve({ ...d, schema: 3 }), false);
  assert.equal(validEvolve({ ...d, nodes: [{ id: 'x', species: 'alien', genes: {}, train: {}, test: {} }] }), false);
  assert.equal(validEvolve(null), false);
});

test('loop shows the six phases and the generation; stats and fitness chart render', () => {
  const loop = loopSVG(d);
  for (const p of ['观察波动', '交叉 · 突变', '训练集回测', '样本外检验', '选择淘汰', '部署冠军']) assert.match(loop, new RegExp(p));
  assert.match(loop, new RegExp(`>${d.generation}<`));
  assert.match(statsMarkup(d), /种群[\s\S]*60/); assert.match(statsMarkup(d), /本代样本外合格/);
  const fit = fitnessSVG(d.history);
  assert.equal((fit.match(/<polyline/g) || []).length, 2); assert.match(fit, /第 1 代/);
  assert.match(fitnessSVG([]), /等待第一代/);
});

test('gene pool: one node per strategy, species bands, lineage edges, champion ring, selection highlight', () => {
  const pos = graphLayout(d);
  assert.equal(Object.keys(pos).length, d.nodes.length);
  const born = Object.fromEntries(d.nodes.map(n => [n.id, n.born]));
  const [a, b] = d.nodes.filter(n => n.born !== d.nodes[0].born).slice(0, 1).concat(d.nodes[0]);
  assert.ok((pos[a.id].x > pos[b.id].x) === (born[a.id] > born[b.id]));          // later generations further right
  const svg = graphSVG(d, d.champion.id);
  assert.equal((svg.match(/data-node="/g) || []).length, d.nodes.length);
  assert.equal((svg.match(/class="champ-ring"/g) || []).length, 1);
  assert.ok((svg.match(/class="evo-edge/g) || []).length > 0);
  assert.match(svg, /evo-node[^"]*\bsel\b/); assert.match(svg, /动量突破 · \d+/);
});

test('node detail: genes with bounds, training vs out-of-sample metrics, parents clickable', () => {
  const child = d.nodes.find(n => n.final && n.parents.length === 2 && n.parents.every(p => d.nodes.some(x => x.id === p)));
  const html = detailMarkup(d, child.id);
  assert.match(html, /训练集 60%/); assert.match(html, /样本外 20%/); assert.match(html, /终审 20%/); assert.match(html, /训练期按行情分/);
  assert.match(html, /收益[\s\S]*最大回撤[\s\S]*交易次数[\s\S]*胜率/);
  for (const p of child.parents) assert.match(html, new RegExp(`data-node="${p}"`));
  assert.match(html, /止损 ×ATR/); assert.match(html, /evo-gbar/);
  assert.match(html, child.qualified ? /样本外与终审都合格/ : /不合格/);
  const old = d.nodes.find(n => !n.final);                                            // scored before the final window existed
  if (old) { assert.match(detailMarkup(d, old.id), /样本外 40%/); assert.doesNotMatch(detailMarkup(d, old.id), /终审 20%/); }
  assert.match(detailMarkup(d, d.champion.id), /冠军组合领头 · 待下次更新/);
  assert.match(detailMarkup(d, d.team[1].id), /冠军组合成员 · 待下次更新/);
  assert.match(detailMarkup(d, 'nope'), /点击基因池/);
});

test('paper account, home card and data caption', () => {
  assert.match(paperMarkup(d), /纸面权益/); assert.match(paperMarkup(d), /<h4>冠军组合<\/h4>/);
  for (const t of d.team) assert.match(paperMarkup(d), new RegExp(`data-node="${t.id}"`));
  assert.match(paperMarkup({ ...d, paper: null }), /还没有样本外合格的策略/);
  assert.match(paperMarkup({ ...d, champion: null, paper: { ...d.paper, members: [] } }), /持有现金/);
  const pm = paperMarkup(d);
  assert.match(pm, /最短任期 7 天/); assert.match(pm, /已有持仓不强平/); assert.match(pm, /连续 3 次样本外合格/); assert.match(pm, /补入空位|沿用原冠军/); assert.match(pm, /席位 1 ·/);
  assert.match(pm, /同一物种最多 2 个/); assert.match(homeCardMarkup(d), /冠军组合 8 个/);
  assert.match(pm, /挑战者[\s\S]*连续合格 <b>\d+<\/b> \/ 3/);
  assert.match(homeCardMarkup(d), new RegExp(`第 ${d.generation} 代`));
  assert.match(dataCaption(d), /训练集[\s\S]*60%[\s\S]*样本外[\s\S]*终审[\s\S]*只决定谁能上场/); assert.match(dataCaption(d), /终审：收益 > 0%/); assert.match(dataCaption(d), /交易 ≥ 8 次/);
  assert.match(miniCurve([{ pts: [[1, 100], [2, 110]], cls: 'evo-test', label: '样本外' }]), /样本外 \+10\.0%/);
});

test('text from the data is escaped', () => {
  const evil = structuredClone(d); evil.nodes[0].summary = '<img src=x onerror=alert(1)>';
  assert.doesNotMatch(detailMarkup(evil, evil.nodes[0].id), /<img/);
});

test('replay steps through every archived generation; nodes appear when born and fade when eliminated', () => {
  const gens = replayGenerations(d);
  assert.deepEqual(gens, [...gens].sort((a, b) => a - b)); assert.equal(new Set(gens).size, gens.length);
  assert.equal(gens.at(-1), Math.max(...d.nodes.map(n => n.born)));
  assert.deepEqual(replayState({ born: '3' }, 2), { future: true, gone: false, newborn: false });
  assert.deepEqual(replayState({ born: '3' }, 3), { future: false, gone: false, newborn: true });
  assert.deepEqual(replayState({ born: '3', died: '5' }, 6), { future: false, gone: true, newborn: false });
  const delays = replayDelays(d, gens, 9000);
  assert.equal(delays.length, gens.length); assert.ok(delays.every(x => x >= 180 && x <= 1200));
  const svg = graphSVG(d, null);
  assert.equal((svg.match(/<g class="evo-node[^"]*" data-node="[^"]+" data-born="\d+"/g) || []).length, d.nodes.length);
  assert.ok((svg.match(/class="evo-edge[^"]*\bflow\b/g) || []).length > 0);   // lines into living strategies carry sparks
  assert.match(svg, /<canvas class="evo-fx"/);                                  // ambient motion layer over the static SVG
  assert.match(svg, /class="evo-gen-label"/);
});

test('ambient motion: sparks travel parent → child then rest, halos breathe, rhythms are stable per item', async () => {
  const { sparkAt, breath, seed, easeInOut } = await import('../assets/evolve-fx.mjs');
  assert.equal(sparkAt(0, 10, 0).u, 0); assert.equal(sparkAt(0, 10, 0).a, 0);
  const mid = sparkAt(1.9, 10, 0); assert.ok(Math.abs(mid.u - .5) < .01); assert.ok(mid.a > .99);
  assert.equal(sparkAt(5, 10, 0), null);                                    // resting for the rest of the period
  let prev = -1; for (let t = 0; t < 3.8; t += .1) { const p = sparkAt(t, 10, 0); assert.ok(p.u >= prev); prev = p.u; }   // never runs backwards
  assert.ok(breath(0, 6, .25) > .99 && breath(0, 6, .75) < .01);
  assert.equal(seed('s35'), seed('s35')); assert.notEqual(seed('s35'), seed('s36')); assert.ok(seed('x') >= 0 && seed('x') < 1);
  assert.equal(easeInOut(0), 0); assert.equal(easeInOut(1), 1);
});

test('schema 2: slim lineage nodes, details merged from nodes.json when a node is opened', async () => {
  const { validEvolve: ok, validNodeDetails, fullNode, needsDetails, detailMarkup: detail, teamIds: seats } = await import('../assets/evolve.mjs');
  const seated = new Set([...seats(d), d.champion.id]);
  const slim = { ...d, schema: 2, details: 'nodes.json', nodes: d.nodes.map(n => seated.has(n.id) ? n : Object.fromEntries(Object.entries(n).filter(([k]) => !['genes', 'summary', 'test_curve', 'train_curve'].includes(k)))) };
  const details = { schema: 1, generation: d.generation, nodes: Object.fromEntries(d.nodes.map(n => [n.id, { genes: n.genes, summary: n.summary, test_curve: n.test_curve }])) };
  assert.ok(ok(slim)); assert.ok(validNodeDetails(details)); assert.equal(validNodeDetails({ schema: 1, generation: 1, nodes: { a: {} } }), false);
  assert.equal(ok({ ...slim, schema: 1 }), false);                                  // schema 1 still requires genes on every node
  const other = slim.nodes.find(n => !seated.has(n.id));
  assert.ok(needsDetails(slim, other.id, null)); assert.equal(needsDetails(slim, other.id, details), false);
  assert.equal(needsDetails(slim, d.champion.id, null), false);                     // seated members carry their genes
  assert.match(detail(slim, other.id, null), /正在读取基因与回测曲线/);
  assert.match(detail(slim, other.id, null, true), /暂时读取失败/);
  const html = detail(slim, other.id, details);
  assert.match(html, /<h4>基因<\/h4>/); assert.equal(fullNode(slim, other.id, details).fitness, other.fitness);
  assert.ok(JSON.stringify(slim).length < JSON.stringify(d).length * .6);
});

test('compat output: schema 1 with genes on every node still passes the pre-nodes.json page check, details load lazily', async () => {
  const { validEvolve: ok, needsDetails, detailMarkup: detail, teamIds: seats } = await import('../assets/evolve.mjs');
  const oldPageCheck = x => x.schema === 1 && x.nodes.every(n => n.genes && n.train && n.test);     // validEvolve before PR #58
  const seated = new Set([...seats(d), d.champion.id]);
  const compat = { ...d, details: 'nodes.json', nodes: d.nodes.map(n => seated.has(n.id) ? n : Object.fromEntries(Object.entries(n).filter(([k]) => !['summary', 'test_curve', 'train_curve'].includes(k)))) };
  const details = { schema: 1, generation: d.generation, nodes: Object.fromEntries(d.nodes.map(n => [n.id, { genes: n.genes, summary: n.summary, test_curve: n.test_curve }])) };
  assert.ok(oldPageCheck(compat)); assert.ok(ok(compat));
  const other = compat.nodes.find(n => !seated.has(n.id));
  assert.ok(needsDetails(compat, other.id, null)); assert.equal(needsDetails(compat, other.id, details), false);
  assert.equal(needsDetails(d, other.id, null), false);                               // plain schema 1 (no details file): nothing to load
  assert.match(detail(compat, other.id, null), /正在读取基因与回测曲线/);
  assert.match(detail(compat, other.id, details), /<h4>基因<\/h4>/);
});

test('team table shows each member\'s direction and the rules mention the direction quota', () => {
  const html = paperMarkup(d);
  assert.match(html, /<li data-node="[^"]+" class="evo-member"[\s\S]*(只做多|只做空|多空)/);
  assert.match(paperMarkup({ ...d, champion_rules: { ...d.champion_rules, direction_cap: 3 } }), /只做多、只做空各最多 3 个/);
  assert.match(paperMarkup({ ...d, paper: { ...d.paper, segments: [{ slot: 4, champion: 's1', species: 'trend', reason: 'balance', from_ts: Date.parse(d.updated_at), to_ts: null }] } }), /方向配额调整/);
});

test('paper positions and trades show contract details: leverage, margin, liquidation, funding', () => {
  const t0 = Date.parse(d.updated_at);
  const paper = { ...d.paper, mode: 'isolated',
    positions: [{ inst: 'PEPE-USDT-SWAP', side: -1, entry: .00001, stop: .0000105, target: null, opened: t0, lever: 10, margin: 123.45, liq: .0000109, funding: -.5, champion: 's1' }],
    trades: [{ inst: 'BTC-USDT-SWAP', side: 1, pnl: -200, reason: 'liquidation', closed: t0, lever: 10, funding: 1.25, champion: 's1', liq: 95.5 }] };
  const html = paperMarkup({ ...d, paper });
  assert.match(html, /10x 逐仓 · 保证金 123\.45 U · 强平/);
  assert.match(html, /资金费 \+0\.50 U/);
  assert.match(html, /强平 · 10x · 强平价 95\.5000 · 资金费 -1\.25/);
  assert.match(html, /保证金占用<\/span><strong class="">123\.45 U/);
  assert.match(html, /纸面交易按逐仓合约规则计算/);
  assert.doesNotMatch(paperMarkup(d), /undefinedx/);                           // older dashboards without contract fields
});

test('the leader is shown with its bigger size and the rules explain it', async () => {
  const { executionProfileText } = await import('../assets/control.mjs');
  const html = paperMarkup(d);
  assert.match(html, /<span class="evo-rank lead">第一名<\/span>[\s\S]*?<b>第一名（信号优先）<\/b>/);
  assert.match(html, new RegExp(`第一名（${d.paper.leader.id}）信号优先[\\s\\S]*风险基因 5%[\\s\\S]*杠杆基因 50×[\\s\\S]*计入同方向上限[\\s\\S]*日亏停手`));
  assert.match(html, /所有席位（含第一名）每笔按风险基因 2%–10%、杠杆基因 1–100×，单笔风险（含主流币加倍）最多 10%/);
  assert.match(html, /BTC\/ETH\/SOL\/BNB\/XRP 维持保证金率 0\.5%，其他币 2\.5%/);

  assert.match(html, /OKX 模拟盘跟单使用同一套风险\/杠杆基因（2%–10%、1–100×），逐仓下单/);

  assert.match(html, /升为第一名（信号优先）/);
  const text = executionProfileText({ execution_profile: { mode: 'evolved', risk: .02, risk_max: .10, same_side_cap: .2, daily_halt: .1, leader_risk: .10, leader_leverage: 100, max_leverage: 100, margin_mode: 'isolated', instruments: ['BTC-USDT-SWAP'], scan_limit: 1 } });
  assert.match(text, /每笔止损风险 2%–10%（按策略）/); assert.match(text, /杠杆最高 100×/); assert.match(text, /逐仓/);
  assert.match(text, /第一名信号优先（计入同向上限和日亏停手）/); assert.match(text, /日亏 10% 停手/);
  assert.doesNotMatch(text, /第一名每笔 10%/);
});


test('shared account: members show what they contributed, positions say 逐仓, rules list the shared limits', () => {
  const html = paperMarkup(d);
  assert.equal(d.paper.mode, 'cross');
  assert.match(html, /贡献 <b class="[a-z]*">[+-]?[\d,.]+ U<\/b>（占账户/); assert.match(html, /样本外 <b[^>]*>[^<]+<\/b> · 终审 <b/); assert.match(html, /共用一个账户/);
  assert.match(html, /每笔开仓都带止损/); assert.match(html, /同方向已有 3 个以上持仓时新开仓风险减半，6 个以上减到四分之一/);
  assert.match(html, /当日亏损达 10% 时全部成员平仓/); assert.match(html, /同样按整个账户算/);
  assert.match(html, /未通过上场门槛/); assert.match(html, /也可立即被合格挑战者替换/);
  const t0 = Date.parse(d.updated_at);
  const one = paperMarkup({ ...d, paper: { ...d.paper, halted: true, positions: [{ inst: 'BTC-USDT-SWAP', side: 1, entry: 100, stop: 95, target: null, opened: t0, lever: 16, margin: 50, funding: 0, champion: 's1', boost: true }],
    trades: [{ inst: 'ETH-USDT-SWAP', side: 1, pnl: -30, reason: 'daily_halt', closed: t0, lever: 10, funding: 0, champion: 's2' }] } });
  assert.match(one, /16x 逐仓 · 保证金 50\.00 U · <b>第一名（信号优先）<\/b>/); assert.doesNotMatch(one, /强平 —/);
  assert.match(one, /日亏停手平仓/); assert.match(one, /今日已停手/);
});

test('the bigger size needs a member that passed both gates; without one the page says so', () => {
  assert.match(paperMarkup(d), /只有通过上场门槛（样本外与终审合格、连续守擂 5 代、回撤 ≤ 30%）的成员才能当第一名/);
  const none = paperMarkup({ ...d, paper: { ...d.paper, leader: null, members: d.paper.members.map(m => ({ ...m, boost: false })) } });
  assert.match(none, /暂无通过上场门槛的成员，已有持仓走完后持有现金/); assert.doesNotMatch(none, /<b>加大仓位<\/b>/); assert.match(none, /暂无第一名/);
});

test('the rules name the five majors and their doubled risk', async () => {
  const { executionProfileText } = await import('../assets/control.mjs');
  assert.match(paperMarkup(d), /BTC \/ ETH \/ SOL \/ XRP \/ DOGE 这 5 个主流币每笔风险 ×2，但仍不超过 10%/);
  const text = executionProfileText({ execution_profile: { mode: 'evolved', risk: .01, risk_max: .05, same_side_cap: .2, daily_halt: .1, majors: ['BTC-USDT-SWAP', 'ETH-USDT-SWAP'], major_factor: 2, instruments: ['BTC-USDT-SWAP'], scan_limit: 1 } });
  assert.match(text, /主流币（BTC\/ETH）风险 ×2/);
});

test('coins whose demo price is off are listed, and a replaced unqualified member is explained', () => {
  const html = paperMarkup(d);
  assert.ok(d.paper.price_off.length > 0);
  assert.match(html, new RegExp(`模拟盘价格偏离真实行情超过 1% 的币暂不开新仓（现在 ${d.paper.price_off.length} 个`));
  assert.match(html, /原成员连续不合格/); assert.equal(d.champion_rules.team_size, 8);
  assert.doesNotMatch(paperMarkup({ ...d, paper: { ...d.paper, price_off: [] } }), /暂不开新仓/);
});

test('phone layout: rules are folded behind a one-line digest, the curve shows its range, the home card names the first place', () => {
  const html = paperMarkup(d);
  assert.match(html, /<details class="evo-more evo-rules-fold"><summary>规则：8 个策略共用一个账户 · 第一名风险 5%、杠杆 50× · 风险 2–10% · 杠杆 1–100× · 单笔最多 10% · 主流币 ×2 · 日亏 10% 停手/);
  assert.match(html, /evo-curve-range[\s\S]*最低 [\d,.]+ · 最高 [\d,.]+ · 现在 [\d,.]+/);
  assert.doesNotMatch(html, /<table class="evo-metrics evo-team">/);
  const lead = d.team.find(t => t.id === d.paper.leader.id);
  assert.match(homeCardMarkup(d), new RegExp(`第一名 ${lead.id}`));
  assert.match(homeCardMarkup({ ...d, paper: { ...d.paper, leader: null } }), new RegExp(`领头 ${d.champion.id}`));
});

test('missing entry_gate fields stay compatible and say 待下次更新', () => {
  assert.equal(entryGateView(d.champion).pending, true);
  assert.match(entryGateMarkup(d.champion), /上场门槛 · 待下次更新/);
  const gated = { ...d.champion, entry_gate: { passed: true, oos_ok: true, streak: 5, streak_needed: 5, max_dd: 12.3, max_dd_limit: 30, reasons: [] } };
  const html = entryGateMarkup(gated);
  assert.match(html, /样本外 通过/); assert.match(html, /守擂 5\/5 代/); assert.match(html, /最大回撤 12\.3%（≤30%）/);
  const fail = entryGateMarkup({ ...d.champion, entry_gate: { passed: false, oos_ok: true, streak: 2, streak_needed: 5, max_dd: 31, max_dd_limit: 30, reasons: ['连续守擂 2/5 代，未满 5 代', '最大回撤 31.0% 超过 30%'] } });
  assert.match(fail, /未满 5 代/); assert.match(fail, /class="evo-gate fail"/);
  const patch = (node, gate, trading) => ({ ...d, champion: node.id === d.champion.id ? { ...node, entry_gate: gate } : d.champion,
    nodes: d.nodes.map(n => n.id === node.id ? { ...n, entry_gate: gate } : n),
    team: d.team.map(m => m.id === node.id ? { ...m, entry_gate: gate } : m),
    paper: { ...d.paper, members: (d.paper.members || []).map(m => m.id === node.id ? { ...m, trading } : m) } });
  assert.match(detailMarkup(patch(gated, gated.entry_gate, true), gated.id), /纸面交易中/);
  const failGate = { ...gated.entry_gate, passed: false, streak: 2 };
  assert.match(detailMarkup(patch(gated, failGate, false), gated.id), /未接入模拟盘/);
});

test('update schedule uses the 5-hour warning and Phnom Penh last/next labels', () => {
  const last = Date.parse('2026-11-12T08:17:00Z');
  const fresh = updateSchedule(last, last + 60 * 60 * 1000);
  assert.equal(fresh.stale, false);
  assert.match(fresh.pill, /1 小时前更新/);
  assert.match(fresh.line, /上次更新/);
  assert.match(fresh.line, /下次更新/);
  assert.match(fresh.line, /金边时间/);
  assert.equal(fresh.warn, '');
  const stale = updateSchedule(last, last + PAPER_STALE_MS + 1000);
  assert.equal(stale.stale, true);
  assert.match(stale.warn, /已超过 5 小时没有更新/);
  const next = nextPaperUpdate(Date.parse('2026-11-12T08:00:00Z'));
  assert.equal(next, Date.parse('2026-11-12T08:17:00Z'));
  const later = nextPaperUpdate(Date.parse('2026-11-12T08:17:00Z'));
  assert.equal(later, Date.parse('2026-11-12T12:17:00Z'));
});

test('evolve intro is one sentence plus key numbers, with the full explanation folded', () => {
  const html = evolveIntroMarkup(d);
  assert.match(html, /第 <b>\d+<\/b> 代/);
  assert.match(html, /冠军样本外/);
  assert.match(html, /<details class="evo-more"><summary>了解进化规则<\/summary>/);
  assert.match(html, /每个席位各自累计守擂/);
  assert.match(evolveIntroMarkup(null), /数据加载中/);
});

test('recent trades list and counts use the current shared account only, not legacy switch closes', () => {
  const from = d.paper.account_from;
  assert.ok(from);
  const legacy = (d.paper.trades || []).filter(t => t.closed <= from);
  assert.ok(legacy.length > 0);
  assert.ok(legacy.some(t => t.reason === 'switch'));
  assert.equal(currentAccountTrades(d.paper).length, (d.paper.trades || []).filter(t => t.closed > from).length);
  const html = paperMarkup(d);
  assert.doesNotMatch(html, /换冠军平仓/);
  assert.match(html, new RegExp(`已平仓</span><strong class="">${currentAccountTrades(d.paper).length}`));
  const t0 = Date.parse(d.updated_at);
  const mixed = paperMarkup({ ...d, paper: { ...d.paper, trades: [
    { inst: 'ETH-USDT-SWAP', side: 1, pnl: -30, reason: 'switch', closed: from - 3600000, lever: 10, funding: 0, champion: 'old' },
    { inst: 'BTC-USDT-SWAP', side: 1, pnl: 4, reason: 'target', closed: t0, lever: 10, funding: 0, champion: 's1' },
  ] } });
  assert.match(mixed, /止盈/);
  assert.doesNotMatch(mixed, /换冠军平仓/);
  assert.match(mixed, /已平仓<\/span><strong class="">1/);
  assert.equal(currentAccountTrades({ trades: [
    { closed: from - 1, reason: 'switch' }, { closed: from + 1, reason: 'target' },
  ], account_from: from }).map(t => t.reason).join(), 'target');
});

test('each seat card shows its own defense streak from entry_gate', () => {
  const team = d.team.map((m, i) => ({ ...m, entry_gate: { passed: i === 0, oos_ok: true, streak: i === 0 ? 5 : 2, streak_needed: 5, max_dd: 10, max_dd_limit: 30, reasons: i === 0 ? [] : ['连续守擂 2/5 代，未满 5 代'] } }));
  const paper = { ...d.paper, members: (d.paper.members || []).map((m, i) => ({ ...m, entry_gate: team[i]?.entry_gate })) };
  const html = paperMarkup({ ...d, team, paper, nodes: d.nodes.map(n => {
    const t = team.find(x => x.id === n.id); return t ? { ...n, entry_gate: t.entry_gate } : n;
  }), champion: { ...d.champion, entry_gate: team[0].entry_gate } });
  assert.match(html, /守擂 5\/5 代/);
  assert.match(html, /守擂 2\/5 代/);
});


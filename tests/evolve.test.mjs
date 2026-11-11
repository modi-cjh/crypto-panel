import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { replayGenerations, replayState, replayDelays, validEvolve, loopSVG, statsMarkup, fitnessSVG, graphLayout, graphSVG, detailMarkup, paperMarkup, homeCardMarkup, dataCaption, miniCurve } from '../assets/evolve.mjs';

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
  assert.match(statsMarkup(d), /种群[\s\S]*40/); assert.match(statsMarkup(d), /本代样本外合格/);
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
  const child = d.nodes.find(n => n.parents.length === 2 && n.parents.every(p => d.nodes.some(x => x.id === p)));
  const html = detailMarkup(d, child.id);
  assert.match(html, /训练集 70%/); assert.match(html, /样本外 30%/);
  assert.match(html, /收益[\s\S]*最大回撤[\s\S]*交易次数[\s\S]*胜率/);
  for (const p of child.parents) assert.match(html, new RegExp(`data-node="${p}"`));
  assert.match(html, /止损 ×ATR/); assert.match(html, /evo-gbar/);
  assert.match(html, child.qualified ? /样本外合格/ : /样本外不合格/);
  assert.match(detailMarkup(d, d.champion.id), /冠军组合领头 · 纸面交易中/);
  assert.match(detailMarkup(d, d.team[1].id), /冠军组合成员 · 纸面交易中/);
  assert.match(detailMarkup(d, 'nope'), /点击基因池/);
});

test('paper account, home card and data caption', () => {
  assert.match(paperMarkup(d), /纸面权益/); assert.match(paperMarkup(d), /<h4>冠军组合<\/h4>/);
  for (const t of d.team) assert.match(paperMarkup(d), new RegExp(`data-node="${t.id}"`));
  assert.match(paperMarkup({ ...d, champion: null }), /还没有样本外合格的策略/);
  const pm = paperMarkup(d);
  assert.match(pm, /最短任期 24 小时/); assert.match(pm, /连续 3 次样本外合格/); assert.match(pm, /补入空位/); assert.match(pm, /席位 1 ·/);
  assert.match(pm, /同一物种最多 2 个/); assert.match(homeCardMarkup(d), /冠军组合 5 个/);
  assert.match(pm, /挑战者[\s\S]*连续合格 <b>\d+<\/b> \/ 3/);
  assert.match(homeCardMarkup(d), new RegExp(`第 ${d.generation} 代`));
  assert.match(dataCaption(d), /训练集[\s\S]*70%[\s\S]*样本外[\s\S]*30%/); assert.match(dataCaption(d), /交易 ≥ 8 次/);
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
  assert.match(html, /<tr data-node="[^"]+" class="evo-member">[\s\S]*(只做多|只做空|多空)/);
  assert.match(paperMarkup({ ...d, champion_rules: { ...d.champion_rules, direction_cap: 3 } }), /只做多、只做空各最多 3 个/);
  assert.match(paperMarkup({ ...d, paper: { ...d.paper, segments: [{ slot: 4, champion: 's1', species: 'trend', reason: 'balance', from_ts: Date.parse(d.updated_at), to_ts: null }] } }), /方向配额调整/);
});

test('paper positions and trades show contract details: leverage, margin, liquidation, funding', () => {
  const t0 = Date.parse(d.updated_at);
  const paper = { ...d.paper,
    positions: [{ inst: 'PEPE-USDT-SWAP', side: -1, entry: .00001, stop: .0000105, target: null, opened: t0, lever: 10, margin: 123.45, liq: .0000109, funding: -.5, champion: 's1' }],
    trades: [{ inst: 'BTC-USDT-SWAP', side: 1, pnl: -200, reason: 'liquidation', closed: t0, lever: 10, funding: 1.25, champion: 's1' }] };
  const html = paperMarkup({ ...d, paper });
  assert.match(html, /10x 逐仓 · 保证金 123\.45 U · 强平/);
  assert.match(html, /资金费 \+0\.50 U/);
  assert.match(html, /强平 · 10x · 资金费 -1\.25/);
  assert.match(html, /保证金占用<\/span><strong class="">123\.45 U/);
  assert.match(html, /纸面交易按 OKX 合约规则计算/);
  assert.doesNotMatch(paperMarkup(d), /undefinedx/);                           // older dashboards without contract fields
});

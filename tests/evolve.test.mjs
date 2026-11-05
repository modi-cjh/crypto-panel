import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { validEvolve, loopSVG, statsMarkup, fitnessSVG, graphLayout, graphSVG, detailMarkup, paperMarkup, homeCardMarkup, dataCaption, miniCurve } from '../assets/evolve.mjs';

const d = JSON.parse(fs.readFileSync(new URL('./fixtures/evolve-dashboard.json', import.meta.url), 'utf8'));

test('dashboard from the engine is accepted; broken ones are rejected', () => {
  assert.ok(validEvolve(d));
  assert.equal(validEvolve({ ...d, schema: 2 }), false);
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

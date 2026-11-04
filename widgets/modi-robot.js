// MODI 模拟盘机器人 · iPhone 主屏幕小组件（Scriptable）
// 安装：App Store 安装免费 App「Scriptable」→ 右上角 + 新建脚本 → 粘贴本文件全部内容 → 命名“MODI 机器人”
//       → 回到主屏幕长按空白处 → 左上角 + → 搜索 Scriptable → 选小号或中号 → 添加后长按小组件“编辑小组件”
//       → Script 选“MODI 机器人”。点小组件会打开看板。
// 数据：只读公开的账户快照（GitHub paper-data 分支），再尝试读取 OKX 模拟盘公开标记价算实时浮盈；
//       连不上 OKX（例如 4G 被屏蔽）时用快照里的数值。不需要、也不会读取任何密钥。
// iOS 决定小组件的刷新时机，通常 5–15 分钟一次。

const DASHBOARD = 'https://modi-cjh.github.io/crypto-panel/';
const SUMMARY = 'https://raw.githubusercontent.com/modi-cjh/crypto-panel/paper-data/okx_demo/summary.json';
const MARKS = 'https://www.okx.com/api/v5/public/mark-price?instType=SWAP';
const HOUR = 3600000, STALE = 90 * 60000;

// ---------------------------------------------------------------- data (pure; unit-tested in node)
const fmt = (v, d = 2) => Number.isFinite(v) ? v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—';
const signed = (v, d = 2) => Number.isFinite(v) ? (v > 0 ? '+' : '') + fmt(v, d) : '—';
const coinOf = inst => String(inst || '').split('-')[0];

function widgetModel(d, marks, now) {
  marks = marks || {};
  if (!d || !Number.isFinite(Number(d.equity))) return { mode: 'offline', label: '离线', mood: 'offline', equity: null, day: null, positions: [], note: '读不到账户快照' };
  const positions = (d.positions || []).map(p => {
    const side = p.side === 'short' || (p.side !== 'long' && Number(p.contracts) < 0) ? -1 : 1;
    const qty = Math.abs(Number(p.contracts)), ct = Number(p.ctVal), entry = Number(p.avgPx), mark = Number(marks[p.inst]);
    const live = mark > 0 && ct > 0 && qty > 0 && entry > 0;
    return { inst: p.inst, coin: coinOf(p.inst), side, upl: live ? side * qty * ct * (mark - entry) : Number(p.upl) || 0, recorded: Number(p.upl) || 0, live };
  });
  const equity = Number(d.equity) - positions.reduce((s, p) => s + p.recorded, 0) + positions.reduce((s, p) => s + p.upl, 0);
  const dayStart = Number(d.day_start), day = dayStart > 0 ? equity - dayStart : null;
  const upl = positions.reduce((s, p) => s + p.upl, 0);
  const age = now - Date.parse(d.updated_at);
  const recent = (d.recent_actions || []).find(a => ['open', 'close', 'closed_on_exchange'].includes(a.action) && now - Date.parse(a.ts) <= HOUR);
  let mode, label;
  if (!(age <= STALE)) { mode = 'offline'; label = '快照延迟'; }
  else if (d.halted) { mode = 'paused'; label = '日亏停手'; }
  else if (positions.length) { mode = 'watch'; label = `盯盘 · ${positions.length} 仓`; }
  else if (d.paused) { mode = 'paused'; label = '已暂停'; }
  else if (now % HOUR < 4 * 60000) { mode = 'scan'; label = '扫描信号'; }
  else { mode = 'wait'; label = `等信号 · ${Math.max(1, Math.ceil((HOUR - now % HOUR) / 60000))} 分`; }
  let mood = mode === 'offline' ? 'offline' : mode === 'paused' ? 'sleepy' : 'idle';
  if (mood === 'idle' && recent) mood = recent.action === 'open' ? 'excited' : recent.action === 'closed_on_exchange' ? 'worried' : 'happy';
  else if (mood === 'idle' && positions.length && equity > 0) mood = upl / equity >= .002 ? 'happy' : upl / equity <= -.005 ? 'worried' : 'idle';
  const top = [...positions].sort((a, b) => Math.abs(b.upl) - Math.abs(a.upl));
  const note = recent ? `${recent.action === 'open' ? '刚开仓' : '刚平仓'} ${coinOf(recent.inst)}` : positions.length ? `浮盈合计 ${signed(upl)} U` : '空仓，等待信号';
  return { mode, label, mood, equity, day, upl, positions: top, live: positions.some(p => p.live), note, updated: d.updated_at };
}

// ---------------------------------------------------------------- drawing (Scriptable only)
function robotImage(mood, size) {
  const c = new DrawContext(); c.size = new Size(size, size); c.opaque = false; c.respectScreenScale = true;
  const k = size / 120, R = (x, y, w, h) => new Rect(x * k, y * k, w * k, h * k);
  const shell = new Color('#e9f4f6'), line = new Color('#0e6b76'), screen = new Color('#13283d'), ink = new Color('#7ce6f0');
  const fillRound = (rect, r, color) => { const p = new Path(); p.addRoundedRect(rect, r * k, r * k); c.addPath(p); c.setFillColor(color); c.fillPath(); };
  const strokeRound = (rect, r, color, w) => { const p = new Path(); p.addRoundedRect(rect, r * k, r * k); c.addPath(p); c.setStrokeColor(color); c.setLineWidth(w * k); c.strokePath(); };
  // antenna, body, head, screen
  c.setFillColor(line); c.fillRect(R(58, 12, 4, 12)); c.fillEllipse(R(54, 4, 12, 12));
  fillRound(R(38, 72, 44, 30), 14, shell); strokeRound(R(38, 72, 44, 30), 14, line, 3);
  fillRound(R(16, 22, 88, 54), 24, shell); strokeRound(R(16, 22, 88, 54), 24, line, 3);
  fillRound(R(25, 31, 70, 37), 16, screen);
  c.setFillColor(mood === 'worried' ? new Color('#ef5b6b') : new Color('#2fbf7a')); c.fillEllipse(R(56, 82, 8, 8));
  c.setFillColor(ink); c.setStrokeColor(ink); c.setLineWidth(4 * k);
  const stroke = pts => { const p = new Path(); p.move(new Point(pts[0][0] * k, pts[0][1] * k)); for (const [x, y] of pts.slice(1)) p.addLine(new Point(x * k, y * k)); c.addPath(p); c.strokePath(); };
  if (mood === 'happy' || mood === 'excited') { stroke([[38, 52], [44, 44], [50, 52]]); stroke([[70, 52], [76, 44], [82, 52]]); }
  else if (mood === 'worried') { c.fillEllipse(R(37, 40, 13, 13)); c.fillEllipse(R(70, 40, 13, 13)); c.setFillColor(new Color('#60a5fa')); c.fillEllipse(R(92, 30, 8, 12)); }
  else if (mood === 'sleepy') { stroke([[38, 49], [50, 49]]); stroke([[70, 49], [82, 49]]); }
  else if (mood === 'offline') { stroke([[38, 47], [50, 47]]); stroke([[70, 47], [82, 47]]); }
  else { fillRound(R(39, 40, 9, 14), 4.5, ink); fillRound(R(72, 40, 9, 14), 4.5, ink); }
  if (mood === 'excited') { c.setFillColor(new Color('#f6b93b')); c.fillEllipse(R(6, 18, 9, 9)); c.fillEllipse(R(104, 26, 7, 7)); }
  c.setStrokeColor(ink); c.setLineWidth(3 * k);
  if (mood === 'worried' || mood === 'offline') stroke([[52, 62], [56, 59], [60, 62], [64, 59], [68, 62]]);
  else if (mood === 'sleepy') stroke([[54, 61], [66, 61]]);
  else stroke([[52, 58], [60, 63], [68, 58]]);
  return c.getImage();
}

function addText(stack, text, size, color, weight = 'regular') {
  const t = stack.addText(text); t.font = weight === 'bold' ? Font.boldSystemFont(size) : weight === 'medium' ? Font.mediumSystemFont(size) : Font.systemFont(size);
  t.textColor = color; t.lineLimit = 1; t.minimumScaleFactor = .7; return t;
}

async function load() {
  let d = null, marks = {};
  try { const r = new Request(SUMMARY + '?t=' + Math.floor(Date.now() / 60000)); r.timeoutInterval = 10; d = await r.loadJSON(); } catch (e) {}
  if (d && (d.positions || []).length) {
    try { const r = new Request(MARKS); r.headers = { 'x-simulated-trading': '1' }; r.timeoutInterval = 5; const b = await r.loadJSON();
      if (b && b.code === '0') for (const row of b.data || []) marks[row.instId] = Number(row.markPx); } catch (e) {}
  }
  return widgetModel(d, marks, Date.now());
}

async function main() {
  const m = await load(), family = config.widgetFamily || 'medium';
  const dark = Device.isUsingDarkAppearance(), fg = dark ? new Color('#f2f5f8') : new Color('#13283d'), muted = dark ? new Color('#9aa8b6') : new Color('#5b6b7c');
  const up = new Color('#1f9d63'), down = new Color('#d64555'), tone = v => !Number.isFinite(v) ? muted : v >= 0 ? up : down;
  const w = new ListWidget(); w.url = DASHBOARD; w.refreshAfterDate = new Date(Date.now() + 10 * 60000);
  const g = new LinearGradient(); g.colors = dark ? [new Color('#10202f'), new Color('#0b1520')] : [new Color('#f4fbfc'), new Color('#e6f1f4')]; g.locations = [0, 1]; w.backgroundGradient = g;
  w.setPadding(12, 12, 12, 12);
  const row = w.addStack(); row.centerAlignContent();
  const left = row.addStack(); left.layoutVertically(); left.centerAlignContent();
  const img = left.addImage(robotImage(m.mood, family === 'small' ? 54 : 64)); img.imageSize = family === 'small' ? new Size(54, 54) : new Size(64, 64);
  left.addSpacer(2); const chip = addText(left, m.label, 10, muted, 'medium'); chip.centerAlignText();
  row.addSpacer(10);
  const right = row.addStack(); right.layoutVertically();
  addText(right, 'OKX 模拟权益', 10, muted);
  addText(right, m.equity === null ? '—' : fmt(m.equity), family === 'small' ? 18 : 22, fg, 'bold');
  addText(right, `今日 ${signed(m.day)} U`, 11, tone(m.day), 'medium');
  if (family !== 'small') {
    right.addSpacer(4);
    for (const p of m.positions.slice(0, 3)) addText(right, `${p.coin} ${p.side === 1 ? '多' : '空'}  ${signed(p.upl)} U`, 11, tone(p.upl));
    if (!m.positions.length) addText(right, m.note, 11, muted);
  }
  w.addSpacer();
  const foot = addText(w, `${m.live ? '实时浮盈' : '快照数值'} · ${new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })}`, 9, muted);
  foot.rightAlignText();
  if (config.runsInWidget) Script.setWidget(w); else await (family === 'small' ? w.presentSmall() : w.presentMedium());
  Script.complete();
}

if (typeof Script !== 'undefined') main();

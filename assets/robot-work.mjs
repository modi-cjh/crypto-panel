// What the companion is doing right now, derived from the OKX demo account: watching positions, scanning for
// signals after an hourly close, or waiting. Pure functions so the narration can be unit-tested.
const finite = Number.isFinite;
const coin = inst => String(inst ?? '').split('-')[0];
const fmt = (v, d = 2) => finite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—';
const signed = (v, d = 2) => finite(v) ? (v > 0 ? '+' : '') + fmt(v, d) : '—';
const HOUR = 3600000, SCAN_WINDOW = 4 * 60000;

// Strategies act on closed 1H candles; time until the next close.
export const nextBarClose = (now = Date.now()) => HOUR - (now % HOUR);

// positions: [{inst, side (1|-1), upl, ours}], from the live-marked account.
export function workState({ positions = [], equity = NaN, paused = false, halted = false, offline = false } = {}, now = Date.now()) {
  if (offline) return { mode: 'offline', label: '离线', detail: '账户数据暂时读不到' };
  if (halted) return { mode: 'paused', label: '停手', detail: '今日触发日亏停手，不再开新仓' };
  const upl = positions.reduce((s, p) => s + (finite(p.upl) ? p.upl : 0), 0);
  const tone = !positions.length ? 'flat' : upl > 0 ? 'up' : upl < 0 ? 'down' : 'flat';
  if (positions.length) {
    const top = [...positions].sort((a, b) => Math.abs(b.upl || 0) - Math.abs(a.upl || 0))[0];
    return { mode: 'watch', tone, upl, label: `盯盘 · ${positions.length} 仓`,
      detail: `${paused ? '新开仓已暂停 · ' : ''}盯着 ${positions.length} 个仓位，合计浮盈 ${signed(upl)} U；波动最大的是 ${coin(top.inst)}（${signed(top.upl)} U）` };
  }
  if (paused) return { mode: 'paused', label: '暂停', tone, detail: '新开仓已暂停，等你恢复' };
  const left = nextBarClose(now), sinceClose = HOUR - left;
  if (sinceClose < SCAN_WINDOW) return { mode: 'scan', label: '扫描信号', tone, detail: '1 小时 K 线刚收盘，正在按三套策略扫描入场信号' };
  const mins = Math.max(1, Math.ceil(left / 60000));
  return { mode: 'wait', label: `等信号 · ${mins} 分`, tone, detail: `空仓中，${mins} 分钟后 1 小时 K 线收盘，再扫描一次信号${finite(equity) ? `（权益 ${fmt(equity)} U）` : ''}` };
}

// Face nudged by the floating PnL when nothing more specific is happening.
export function pnlMood(upl, equity) {
  if (!finite(upl) || !finite(equity) || equity <= 0) return null;
  const r = upl / equity;
  return r >= .002 ? 'happy' : r <= -.005 ? 'worried' : null;
}

// A one-shot "ticket" for a new exchange action. prevUpl: inst -> last floating PnL before the action.
export function tradeBurst(a, prevUpl = new Map()) {
  const c = coin(a.inst), last = prevUpl.get(a.inst);
  if (a.action === 'open') {
    const long = a.side === 1;
    return { kind: long ? 'open-long' : 'open-short', title: `${long ? '开多' : '开空'} ${c}`, detail: `${a.contracts} 张 @ ${a.price}`, mood: 'excited' };
  }
  if (a.action === 'close' || a.action === 'closed_on_exchange') {
    const pnl = finite(Number(a.net_pnl)) ? Number(a.net_pnl) : finite(Number(a.pnl)) ? Number(a.pnl) : finite(last) ? last : null;
    const win = pnl !== null && pnl >= 0;
    const what = a.action === 'closed_on_exchange' ? (pnl === null ? `${c} 止损 / 止盈触发` : win ? `${c} 止盈触发` : `${c} 止损触发`) : `平仓 ${c}`;
    return { kind: pnl === null ? 'close' : win ? 'close-win' : 'close-loss', title: what, amount: pnl === null ? null : `${signed(pnl)} U`,
      detail: pnl === null ? null : a.net_pnl !== undefined ? '含手续费' : '按平仓前浮盈估算', mood: pnl === null ? 'happy' : win ? 'happy' : 'sad' };
  }
  if (a.action === 'daily_halt') return { kind: 'close-loss', title: '日亏停手', detail: '全部平仓，今天不再开新仓', mood: 'sad' };
  return null;
}

// Spoken summary for a double tap.
export function positionReport(positions = []) {
  if (!positions.length) return '现在空仓，我在等下一根 1 小时 K 线收盘再扫描信号。';
  const rows = [...positions].sort((a, b) => Math.abs(b.upl || 0) - Math.abs(a.upl || 0))
    .map(p => `${coin(p.inst)} ${p.side === 1 ? '多' : '空'} ${signed(p.upl)}U`);
  const total = positions.reduce((s, p) => s + (finite(p.upl) ? p.upl : 0), 0);
  return `持仓 ${positions.length} 个：${rows.join('、')}；合计 ${signed(total)}U。`;
}

// Occasional commentary when the floating PnL moves a lot. memo: {upl, at} of the last comment (mutated).
export function pnlComment(memo, { upl, equity, positions = [] }, now = Date.now()) {
  if (!positions.length || !finite(upl)) { memo.upl = null; return null; }
  if (memo.upl === null || memo.upl === undefined) { memo.upl = upl; memo.at = now; return null; }
  const step = Math.max(5, finite(equity) ? equity * .002 : 5), moved = upl - memo.upl;
  if (Math.abs(moved) < step || now - (memo.at || 0) < 120000) return null;
  memo.upl = upl; memo.at = now;
  const top = [...positions].sort((a, b) => (moved > 0 ? (b.upl || 0) - (a.upl || 0) : (a.upl || 0) - (b.upl || 0)))[0];
  return moved > 0
    ? { text: `浮盈回升到 ${signed(upl)}U，${coin(top.inst)} 出力最多`, mood: 'happy' }
    : { text: `浮亏扩大到 ${signed(upl)}U，${coin(top.inst)} 拖累最多；止损都挂在交易所`, mood: 'worried' };
}

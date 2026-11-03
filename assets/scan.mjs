import { mainAccounts, validManualStatus } from './account.mjs';
import { pendingOrders } from './portfolio.mjs';
const symbol = value => typeof value === 'string' && /^[A-Z0-9]+-USDT-SWAP$/.test(value);
export const strategyName = name => ({mean_reversion:'均值回归',breakout:'突破放量',ema:'EMA 趋势',manual:'会议单'})[name] || name;
export function signalText(row) {
  const signals = Object.entries(row.signals || {}).filter(([name])=>['mean_reversion','manual'].includes(name)).filter(([,side]) => side === 1 || side === -1);
  return signals.length ? signals.map(([s,side]) => `${strategyName(s)} ${side === 1 ? '多' : '空'}`).join(' / ') : row.signal_status === 'ok' ? '无' : '数据不足';
}
export function validScan(data) {
  const finiteOrNull = n => n === null || Number.isFinite(n);
  return data?.schema_version === 1 && (data.generated_at === null || Number.isFinite(Date.parse(data.generated_at))) &&
    Array.isArray(data.instruments) && data.instruments.every(r => symbol(r.instId) && ['quote_24h','spread','leverage_cap'].every(k => finiteOrNull(r[k])) &&
      typeof r.held === 'boolean' && r.signals && typeof r.signals === 'object' && Object.values(r.signals).every(s => s === null || s === 1 || s === -1)) &&
    Array.isArray(data.recent_signals) && data.recent_signals.every(r => symbol(r.instId) && Number.isFinite(Date.parse(r.timestamp)) && ['mean_reversion','breakout','manual'].includes(r.strategy) && [1,-1].includes(r.side) && typeof r.opened === 'boolean' && (r.reason === null || typeof r.reason === 'string')) &&
    Array.isArray(data.positions) && data.positions.every(r => symbol(r.instId)) && validManualStatus(data.manual_status) && validManualStatus(data.pending_orders);
}
export function scanRows(data, filter = 'all', key = 'quote_24h', direction = -1) {
  return (data?.instruments || []).filter(r => filter === 'held' ? r.held : filter === 'signal' ? ['mean_reversion','manual'].some(k=>[1,-1].includes(r.signals[k])) : true).sort((a,b) => {
    const value = r => key === 'signal' ? signalText(r) : key === 'held' ? Number(r.held) : r[key];
    const av = value(a), bv = value(b);
    if (av == null) return bv == null ? a.instId.localeCompare(b.instId) : 1;
    if (bv == null) return -1;
    return (typeof av === 'string' ? av.localeCompare(bv) : av-bv)*direction || a.instId.localeCompare(b.instId);
  });
}
// Bare tokens such as "P3", "RSI" or "FOMC" are only accepted when they name a
// known instrument; an unknown instId would break the shared WebSocket subscription.
export function meetingSymbol(meeting, fallback, known = null) {
  const full = typeof meeting === 'string' && meeting.match(/\b[A-Z][A-Z0-9]*-USDT-SWAP\b/);
  if (full) return full[0];
  if (!known?.size) return fallback;
  const coin = typeof meeting === 'string' && meeting.match(/\b[A-Z][A-Z0-9]{1,14}\b/g)?.find(s => known.has(s+'-USDT-SWAP'));
  return coin ? coin+'-USDT-SWAP' : fallback;
}
export function chartUniverse(account, scan, meeting) {
  const holdings = mainAccounts(account).filter(a => !['未启动','已停止'].includes(a.status)).flatMap(a => a.positions);
  const positions = [...holdings, ...(!account ? (scan?.positions || []).map(p => ({...p,symbol:p.instId})) : [])];
  positions.sort((a,b) => (Date.parse(b.entry_time)||0)-(Date.parse(a.entry_time)||0));
  const held = new Set(positions.map(p => p.symbol));
  const signaled = new Set([...(scan?.recent_signals || []).filter(r=>['mean_reversion','manual'].includes(r.strategy)).map(r => r.instId), ...(scan?.instruments || []).filter(r => ['mean_reversion','manual'].some(k=>[1,-1].includes(r.signals[k]))).map(r => r.instId)]);
  const orders=pendingOrders(account,scan);
  const ids = [...new Set([...held,...orders.map(p=>p.symbol),...signaled,meeting])].filter(symbol);
  return {defaultSymbol:positions[0]?.symbol || orders.find(p=>symbol(p.symbol))?.symbol || meeting, choices:ids.map(id => ({id,held:held.has(id),pending:orders.some(p=>p.symbol===id),positions:positions.filter(p=>p.symbol===id),orders:orders.filter(p=>p.symbol===id),signal:signaled.has(id),meeting:id === meeting}))};
}
export function strategyEntries(account) {
  const enabled = account?.enabled_strategies || Object.keys(account?.strategies || {});
  return enabled.filter(name=>['mean_reversion','manual'].includes(name)).map(name => ({name, mode:account?.entry_mode_by_strategy?.[name] || null}));
}
export const reasonText = code => ({blackout:'事件窗口禁开新仓',invalid_decisions:'决议无效',bias_long:'决议仅允许多头',bias_short:'决议仅允许空头',bias_watch:'会议决议观察',bias_ban:'会议决议禁开仓',size_factor_zero:'仓位系数为零',strategy_disabled:'策略未启用',already_held:'已有持仓',pending_order:'已有挂单',daily_halted:'当日停手',maker_pending:'maker 待成交',expired:'maker 到期未成交',stale_signal:'信号超过入场期限',missing_price:'缺少现价',initialize_only:'仅初始化',entry_mode_changed:'入场方式已更改',risk_or_margin_or_minimum:'风险 / 保证金 / 最小数量限制',same_direction_risk:'同向风险上限',stop_distance:'止损距离限制',risk_or_universe:'风险或币池限制'})[code] || code || '—';

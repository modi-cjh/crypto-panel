import {okxActionsMarkup} from './okx.mjs';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const at=t=>Number.isFinite(Date.parse(t))?new Date(t).toLocaleString('zh-CN',{timeZone:'Asia/Bangkok',hour12:false}):'时间待确认';
const coin=s=>String(s||'全部').replace('-USDT-SWAP','');
const strategies={mean_reversion:'均值回归',breakout_long:'小时趋势突破',breakout_daily:'日线趋势突破'};
const reasons={same_side_risk:'同向风险已达上限',same_side_count:'同向持仓数已达上限',max_positions:'达到持仓数量上限',daily_halt:'触发日亏停手',already_at_mean:'价格已回到均值',insufficient_margin:'保证金不足'};
const kinds={breakout:'价格突破',volume:'成交量',volume_spike:'放量',funding:'资金费率',funding_extreme:'资金费率',oi:'持仓量',oi_change:'持仓量',price:'价格',price_change:'价格波动',market:'市场异动'};

export function decisionsMarkup({summary,history,status='loading'}={}){
 const records=(summary?.strategy_decisions||[]).slice(0,30);
 const updated=summary?.strategy_updated_at;
 const current=records.map(r=>`<li><time>${esc(at(r.ts))} UTC+7</time><strong>${esc(coin(r.inst))} · ${r.type==='signal'?'出现策略信号':r.type==='daily_halt'?'日亏停手':'跳过开仓'}</strong><span>${esc(strategies[r.strategy]||'风控')}${r.side===1?' · 多':r.side===-1?' · 空':''}${r.reason?' · '+esc(reasons[r.reason]||'其他规则限制'):''}</span></li>`).join('');
 const groups=(history?.alert_groups||[]).slice(0,20);
 const archived=groups.map(g=>`<details class="decision-archive"><summary>异动 #${esc(g.wake_index??'—')} · ${(g.alerts||[]).length} 条 · ${g.adopted?'有关联会议单':'未采纳'}</summary><p class="muted">${esc(at(g.timestamp))} UTC+7</p><ul>${(g.alerts||[]).map(a=>`<li>${esc(coin(a.symbol))} · ${esc(kinds[a.type]||'市场异动')} · ${a.adopted?'已关联':'未关联会议单'}</li>`).join('')}</ul>${(g.orders||[]).map(o=>`<p>${esc(coin(o.symbol))} · ${o.match==='exact'?'精确引用':'同币时间候选（不代表因果）'} · ${o.closed_time&&Number.isFinite(o.pnl)?'最终净盈亏 '+esc(o.pnl)+' USDT':'尚无最终平仓结果'}</p>`).join('')}</details>`).join('');
 return `<h3>当前策略决策</h3><p class="muted">${updated?'最近扫描 '+esc(at(updated))+' UTC+7':'扫描时间等待任务回执'}。当前策略按固定规则产生信号，不是原异动会议系统。</p>`+
  `<ol class="decision-records">${current||'<li class="empty">本次公开记录中没有策略信号或跳过记录，不代表扫描任务未运行。</li>'}</ol>`+
  `<h3>OKX 模拟执行反馈</h3><ol class="fills-list">${summary?okxActionsMarkup(summary,10):'<li class="empty">等待模拟账户执行回执</li>'}</ol>`+
  `<h3>原异动 → 会议决策（历史）</h3><p class="muted">旧 crypto-bot 数据源已暂停。${history?'最后快照 '+esc(at(history.generated_at))+' UTC+7；这些不是当前实时异动。':status==='error'?'历史记录暂时读取失败，稍后重试。':'正在读取原始异动记录…'}</p>`+
  (archived|| (history?'<p class="empty">历史快照没有异动记录。</p>':''));
}

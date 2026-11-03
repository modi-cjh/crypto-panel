import { coinIcon } from './coins.mjs';
import {localDay,isolatedPnL} from './account.mjs';
const finite=Number.isFinite;
export const closeReason=code=>({stop:'止损',stop_live:'止损',stop_loss:'止损',target:'止盈',target_live:'止盈',take_profit:'止盈',timeout:'到期',expired:'到期',manual:'手动',manual_close:'手动',paused_flatten:'手动（暂停平仓）',decision_close:'手动',decision_ban:'手动（决议禁开）',liquidation_live:'强平',liquidation:'强平',liquidated:'强平'})[code] || code || '—';
export function equityRange(points,range='all',now=Date.now()) {
  const cutoff=range==='day'?now-86400000:range==='week'?now-7*86400000:-Infinity;
  const unique=new Map(points.filter(p=>finite(p.equity) && finite(Date.parse(p.timestamp)) && Date.parse(p.timestamp)<=now).map(p=>[Date.parse(p.timestamp),p]));
  return [...unique].sort(([a],[b])=>a-b).filter(([ts])=>ts>=cutoff).map(([,p])=>p);
}
export function drawdowns(points) {
  let peak=null,max={amount:0,percent:0,start:null,end:null},current={amount:0,percent:0,start:null,end:null};
  for(const p of equityRange(points,'all',Infinity)) {
    if(!peak || p.equity>=peak.equity)peak=p;
    const amount=Math.max(0,peak.equity-p.equity),percent=peak.equity>0?amount/peak.equity*100:null;
    current={amount,percent,start:peak.timestamp,end:p.timestamp};
    if(amount>0 && (max.percent===null || percent!==null && percent>max.percent))max={...current};
  }
  return {max,current:peak?current:null};
}
export function strategyStats(trades) {
  return ['mean_reversion','breakout','manual'].map(strategy=>{
    const rows=trades.filter(t=>t.strategy===strategy && finite(t.pnl));
    const wins=rows.filter(t=>t.pnl>0),losses=rows.filter(t=>t.pnl<0);
    const sum=(list,k)=>list.reduce((n,t)=>n+t[k],0);
    const total=sum(rows,'pnl'),gross=rows.every(t=>finite(t.gross_pnl))?Math.abs(sum(rows,'gross_pnl')):null;
    const fees=rows.every(t=>finite(t.fee))?sum(rows,'fee'):null;
    return {strategy,count:rows.length,winRate:rows.length?wins.length/rows.length*100:null,
      payoff:losses.length && wins.length?(sum(wins,'pnl')/wins.length)/(-sum(losses,'pnl')/losses.length):null,
      average:rows.length?total/rows.length:null,total,feeRatio:gross>0 && fees!==null?fees/gross*100:null};
  });
}
export function riskProgress(account,now=Date.now()) {
  const baseline=account?.day_start;
  const daily=account?.day===localDay(now) && baseline>0 && finite(account.equity)?Math.max(0,(baseline-account.equity)/baseline):null;
  const progress=(ratio,limit)=>({ratio,limit,used:ratio===null?null:Math.min(100,Math.max(0,ratio/limit*100)),remaining:ratio===null?null:Math.max(0,limit-ratio)});
  const positions=account?.positions || [];
  const risk=p=>finite(p.planned_risk) && p.planned_risk>=0?p.planned_risk:finite(p.entry) && finite(p.stop) && finite(p.qty)?Math.abs(p.entry-p.stop)*p.qty:null;
  const direction=side=>{
    const rows=positions.filter(p=>p.side===side),risks=rows.map(risk);
    const amount=risks.every(finite)?risks.reduce((n,r)=>n+r,0):null;
    return {...progress(amount!==null && account?.equity>0?amount/account.equity:null,.05),amount};
  };
  return {daily:progress(daily,.08),long:direction(1),short:direction(-1),stops:positions.map(p=>({...p,distance:finite(p.mark_price) && p.mark_price>0 && finite(p.stop)?p.side*(p.mark_price-p.stop)/p.mark_price*100:null}))};
}
export function blackoutCountdown(windows,now=Date.now()) {
  const valid=(windows || []).filter(w=>finite(Date.parse(w.start)) && Date.parse(w.end)>Date.parse(w.start) && Date.parse(w.end)>now);
  const active=valid.filter(w=>Date.parse(w.start)<=now).sort((a,b)=>Date.parse(a.end)-Date.parse(b.end));
  const window=active[0] || valid.sort((a,b)=>Date.parse(a.start)-Date.parse(b.start))[0];
  return window?{window,active:active.length>0,milliseconds:(active.length?Date.parse(window.end):Date.parse(window.start))-now}:null;
}
export function duration(ms) {
  const seconds=Math.max(0,Math.ceil(ms/1000)),days=Math.floor(seconds/86400),hours=Math.floor(seconds%86400/3600),minutes=Math.floor(seconds%3600/60);
  return (days?days+'天 ':'')+String(hours).padStart(2,'0')+':'+String(minutes).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');
}
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const fmt=(v,d=2)=>finite(v)?v.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const sign=v=>finite(v)?(v>0?'+':'')+fmt(v):'—';
const tone=v=>finite(v)?v>=0?'up':'down':'';
const name=s=>({mean_reversion:'均值回归',breakout:'突破（历史）',manual:'会议单'})[s] || s;
const date=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Bangkok',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const time=t=>finite(Date.parse(t))?date.format(Date.parse(t)):'—';
const $=id=>document.getElementById(id);
const metric=(label,value,cls='')=>`<div><span>${label}</span><strong class="${cls}">${value}</strong></div>`;
export function curveSVG(points,dd) {
  if(!points.length)return '<p class="empty">暂无记录，重新开始后显示</p>';
  const values=points.map(p=>p.equity),lo=Math.min(...values),hi=Math.max(...values),span=hi-lo || Math.max(1,Math.abs(hi)*.01);
  const first=Date.parse(points[0].timestamp),last=Date.parse(points.at(-1).timestamp);
  const xy=p=>[65+(Date.parse(p.timestamp)-first)/(last-first || 1)*610,28+(hi-p.equity)/span*188];
  const path=points.map((p,i)=>(i?'L':'M')+xy(p).join(',')).join(' ');
  const markers=dd.max.amount>0?[dd.max.start,dd.max.end].map((ts,i)=>{const p=points.find(p=>p.timestamp===ts),[x,y]=xy(p);return `<circle cx="${x}" cy="${y}" r="5" fill="${i?'#fb7185':'#06727d'}"><title>${i?'回撤低点':'回撤峰值'} ${esc(time(ts))} ${fmt(p.equity)} USDT</title></circle>`;}).join(''):'';
  return `<svg viewBox="0 0 700 260" role="img" aria-label="主力盘权益曲线，最大回撤 ${fmt(dd.max.percent)}%"><defs><linearGradient id="history-fill" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#06727d" stop-opacity=".25"/><stop offset="1" stop-color="#06727d" stop-opacity="0"/></linearGradient></defs>${[0,.5,1].map(n=>`<line x1="65" x2="675" y1="${28+n*188}" y2="${28+n*188}" stroke="#e5e7eb"/><text x="60" y="${32+n*188}" text-anchor="end">${fmt(hi-n*span)}</text>`).join('')}<path d="${path} L675,226 L65,226 Z" fill="url(#history-fill)"/><path d="${path}" fill="none" stroke="#06727d" stroke-width="2" vector-effect="non-scaling-stroke"/>${markers}<text x="65" y="250">${esc(time(points[0].timestamp).slice(5,16))}</text><text x="675" y="250" text-anchor="end">${esc(time(points.at(-1).timestamp).slice(5,16))} UTC+7</text></svg>`;
}
export class HistoryPage {
  constructor(){this.data=null;this.account=null;this.range='all';this.page=0;this.busy=false;this.bound=false;this.prices=new Map();}
  bind(){
    if(this.bound)return;this.bound=true;
    $('history-range').addEventListener('click',event=>{const b=event.target.closest('[data-range]');if(!b)return;this.range=b.dataset.range;this.renderCurve();});
    for(const id of ['history-symbol','history-strategy'])$(id).addEventListener('change',()=>{this.page=0;this.renderTrades();});
    $('history-prev').addEventListener('click',()=>{this.page--;this.renderTrades();});
    $('history-next').addEventListener('click',()=>{this.page++;this.renderTrades();});
  }
  setAccount(data){this.account=data?.accounts.find(a=>a.id==='paper_main');this.decisions=data?.decisions;this.bind();this.renderRisk();}
  setPrices(rows){this.prices=new Map(rows.filter(r=>finite(r.last) && r.last>0).map(r=>[r.id,r.last]));this.renderRisk();}
  async refresh(){
    this.bind();if(this.busy)return;this.busy=true;
    try{
      const res=await fetch(`./paper/history.json?t=${Date.now()}`,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(12000)});
      if(!res.ok)throw new Error();const data=await res.json();
      if(data.schema_version!==1 || data.account_id!=='paper_main' || !['equity','trades','alert_groups'].every(k=>Array.isArray(data[k])))throw new Error();
      this.data=data;
      $('history-updated').textContent='快照 '+time(data.generated_at)+' UTC+7';
      if(data.historical)$('history-updated').textContent='已停止 · 历史快照 '+time(data.generated_at)+' UTC+7';
      const selected=$('history-symbol').value;
      $('history-symbol').innerHTML='<option value="">全部币</option>'+[...new Set(data.trades.map(t=>t.symbol))].sort().map(s=>`<option value="${esc(s)}">${esc(s.split('-')[0])}</option>`).join('');
      $('history-symbol').value=[...$('history-symbol').options].some(o=>o.value===selected)?selected:'';
      this.renderCurve();this.renderStats();this.renderTrades();this.renderAlerts();this.renderRisk();$('history-error').textContent='';
    }catch{$('history-error').textContent='账户历史暂时读取失败，保留已有数据，稍后自动重试。';}
    finally{this.busy=false;}
  }
  renderCurve(){
    if(!this.data)return;
    const points=equityRange(this.data.equity,this.range),dd=drawdowns(points);
    $('history-range').querySelectorAll('button').forEach(b=>{b.classList.toggle('active',b.dataset.range===this.range);b.setAttribute('aria-pressed',String(b.dataset.range===this.range));});
    $('history-drawdown').innerHTML=points.length?metric('最大回撤',`${fmt(dd.max.amount)} USDT / ${fmt(dd.max.percent)}%`,'down')+metric('起止 UTC+7',dd.max.start?`${time(dd.max.start)} → ${time(dd.max.end)}`:'暂无回撤')+metric('当前回撤',`${fmt(dd.current.amount)} USDT / ${fmt(dd.current.percent)}%`,'down'):'<p class="empty">暂无记录，重新开始后显示</p>';
    $('history-curve').innerHTML=curveSVG(points,dd);
  }
  renderStats(){
    if(!this.data.trades.length){$('history-strategies').innerHTML='<p class="empty">暂无记录，重新开始后显示五项统计</p>';return;}
    $('history-strategies').innerHTML=strategyStats(this.data.trades).map(s=>`<article class="history-card"><h3>${name(s.strategy)}</h3><div class="history-metrics">${metric('已平仓笔数',s.count)}${metric('胜率',fmt(s.winRate)+'%')}${metric('盈亏比',fmt(s.payoff))}${metric('平均每笔',sign(s.average)+' USDT',tone(s.average))}${metric('累计净盈亏',sign(s.total)+' USDT',tone(s.total))}${metric('手续费占比',fmt(s.feeRatio)+'%')}</div>${!s.count?'<small>暂无已平仓交易</small>':''}</article>`).join('');
  }
  renderTrades(){
    if(!this.data)return;
    const rows=this.data.trades.filter(t=>(!$('history-symbol').value || t.symbol===$('history-symbol').value) && (!$('history-strategy').value || t.strategy===$('history-strategy').value));
    const pages=Math.max(1,Math.ceil(rows.length/20));this.page=Math.min(pages-1,Math.max(0,this.page));
    $('history-trades').innerHTML=rows.slice(this.page*20,this.page*20+20).map(t=>`<article class="history-card trade-card"><div class="history-card-head"><h3 class="coin-label">${coinIcon(t.symbol)}${esc(t.symbol.split('-')[0])} <span class="${t.side===1?'up':'down'}">${t.side===1?'多':'空'} · ${fmt(t.leverage,0)}×</span></h3><span class="pill">${name(t.strategy)}</span></div><div class="trade-times"><span>开仓 <time>${time(t.entry_time)}</time></span><span>平仓 <time>${time(t.exit_time)}</time></span><small>UTC+7</small></div><div class="history-metrics">${metric('开仓价',fmt(t.entry,6))}${metric('平仓价',fmt(t.exit,6))}${metric('名义金额 USDT',fmt(t.notional))}${metric('入场保证金 USDT',fmt(t.margin))}${metric('净盈亏 USDT',sign(t.pnl),tone(t.pnl))}${metric('盈亏 % / 保证金',sign(t.pnl_percent)+'%',tone(t.pnl_percent))}</div><div class="trade-reason">平仓原因 <strong>${esc(closeReason(t.reason))}</strong></div></article>`).join('') || '<p class="empty">暂无记录，重新开始后显示</p>';
    $('history-page-number').textContent=`${this.page+1} / ${pages} · ${rows.length} 笔`;
    $('history-prev').disabled=this.page===0;$('history-next').disabled=this.page>=pages-1;
  }
  renderRisk(){
    if(!$('history-risk'))return;
    const a=this.account?{...this.account,positions:this.account.positions.map(p=>({...p,mark_price:this.account.status==='已停止'?p.mark_price:this.prices.get(p.symbol)??p.mark_price}))}:null;
    if(a && a.status!=='已停止' && finite(a.balance)){
      const pnl=a.positions.map(p=>isolatedPnL(p,p.mark_price));
      if(pnl.every(finite))a.equity=a.balance+pnl.reduce((sum,n)=>sum+n,0);
    }
    const risk=riskProgress(a);
    $('history-overview').innerHTML=metric(a?.status==='已停止'?'主力盘历史权益':'主力盘权益',fmt(a?.equity)+' USDT')+metric('现金余额',fmt(a?.balance)+' USDT')+metric('当前持仓',a?.status==='暂停'?'无持仓（已暂停）':a?a.positions.length+' 仓':'—');
    const bar=(label,r)=>`<div class="history-risk-row"><div><span>${label}</span><strong class="${r.used>=100?'down':''}">${r.ratio===null?'—':fmt(r.ratio*100)+'%'} / ${fmt(r.limit*100,0)}%</strong></div><progress max="100" value="${r.used??0}" aria-label="${label}" ${r.used===null?'data-missing="true"':''}></progress><small>${r.ratio===null?'缺少有效基准':`剩余 ${fmt(r.remaining*100)} 个百分点`}${r.amount==null?'':' · 风险 '+fmt(r.amount)+' USDT'}</small></div>`;
    $('history-risk').innerHTML=bar('当日亏损 · 8% 熔断',risk.daily)+bar('多头合计风险 · 5% 上限',risk.long)+bar('空头合计风险 · 5% 上限',risk.short)+`<p class="panel-caption">日亏按 UTC+7 日初权益；同向风险优先 planned_risk，缺失按 |开仓价−止损|×币数重算，再除当前权益。快照标记价回退，行情有效时更新。</p>`;
    $('history-stops').innerHTML=risk.stops.map(p=>metric(`${esc(p.symbol.split('-')[0])} ${p.side===1?'多':'空'} · 距止损`,fmt(p.distance)+'%',p.distance<=0?'down':'' )).join('') || `<p class="muted">${a?.status==='暂停'?'无持仓（已暂停）':'当前无持仓'}</p>`;
    this.renderCountdown();
  }
  renderCountdown(){
    if(!$('history-blackout'))return;
    const c=blackoutCountdown(this.decisions?.blackout_windows);
    $('history-blackout').innerHTML=c?`<span class="eyebrow">${c.active?'当前事件窗口':'下一个事件窗口'}</span><strong class="${c.active?'down':''}">${c.active?'禁开中，剩余 ':'距禁开 '}${duration(c.milliseconds)}</strong><p>${esc(c.window.symbols.join(' / '))}</p><small>${time(c.window.start)} → ${time(c.window.end)} UTC+7</small><p>${esc(c.window.reason || '暂无公开原因')}</p>`:'<span class="muted">暂无即将生效的禁开时段</span>';
  }
  renderAlerts(){
    const labels={breakout:'价格突破',volume:'成交量',volume_spike:'放量',funding:'资金费率',funding_extreme:'资金费率',oi:'持仓量',oi_change:'持仓量',price:'价格',price_change:'价格波动',market:'市场异动'};
    const statuses={pending:'待成交',filled:'已成交',expired:'已到期',rejected:'已拒绝',cancelled:'已取消'};
    $('history-alerts').innerHTML=this.data.alert_groups.map(g=>`<details class="history-card alert-group" open><summary><span>异动 #${g.wake_index??'—'} · ${g.alerts.length} 条</span><span class="${g.adopted?'up':'muted'}">${g.adopted?'有关联会议单':'未采纳'}</span></summary><small>${time(g.timestamp)} UTC+7</small><div class="alert-coins">${g.alerts.map(a=>`<span>${esc(a.symbol.split('-')[0])} · ${labels[a.type] || '市场异动'} <b class="${a.adopted?'up':'muted'}">${a.adopted?'已关联':'未采纳'}</b></span>`).join('')}</div>${g.orders.map(o=>`<div class="alert-order"><strong>${esc(o.symbol.split('-')[0])} · 会议单 ${statuses[o.status] || '状态待更新'}</strong><small>${o.match==='exact'?'精确引用':'24h 同币时间候选'} · ${time(o.time)} UTC+7</small><span class="${tone(o.pnl)}">最终净盈亏 ${o.closed_time?sign(o.pnl)+' USDT':'—（未平仓或暂无成交）'}</span>${o.closed_time?`<small>平仓 ${time(o.closed_time)} UTC+7</small>`:''}</div>`).join('')}</details>`).join('') || '<p class="empty">暂无 wake=true 异动记录</p>';
  }
}

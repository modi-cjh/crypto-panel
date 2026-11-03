import {ROLES} from './team.mjs';
const stamp=v=>Date.parse(v);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
// The clock is an input, so identical snapshots always produce identical states.
export function mascotState(snapshot) {
  const at=stamp(snapshot?.updated_at ?? snapshot?.generated_at),now=snapshot?.now ?? at;
  const account=(snapshot?.accounts || []).find(a=>['paper_main','live_main'].includes(a.id));
  if(!Number.isFinite(at) || !Number.isFinite(now) || !account || snapshot.dataDisconnected || now-at>(snapshot.offlineMinutes??15)*60000)
    return {state:'offline',label:'离线',message:'快照过期或数据中断'};
  const pause=snapshot.pause ?? account.pause;
  if(pause?.paused || account.status==='暂停')return {state:'paused',label:'暂停',message:'模拟盘暂停中'};
  const recent=t=>Number.isFinite(stamp(t)) && now-stamp(t)>=0 && now-stamp(t)<=15*60000;
  const events=snapshot.events || [];
  if(events.some(e=>recent(e.ts) && (e.type==='trade_closed' || e.type==='liquidated') && /止损|stop|强平/.test(e.reason??e.type)))return {state:'sad',label:'刚止损',message:'刚刚止损，按风控计划休息一下'};
  if(events.some(e=>recent(e.ts) && e.type==='trade_opened') || (account.fills||[]).some(f=>f.kind==='open' && recent(f.timestamp)) || (account.positions||[]).some(p=>recent(p.entry_time)))return {state:'excited',label:'刚开仓',message:'刚开仓，正在关注持仓'};
  const wakes=(snapshot.alerts || events).filter(a=>a.wake===true && recent(a.ts));
  if(wakes.length)return {state:'alert',label:'发现异动',message:`刚发现 ${wakes.length} 条异动`};
  const day=new Date(now+7*3600000).toISOString().slice(0,10);
  const pnl=account.day===day?account.daily_pnl:null;
  if(Number.isFinite(pnl) && pnl>0)return {state:'happy',label:'今日盈利',message:`今日 +${Number(pnl.toFixed(2))}U`};
  return {state:'idle',label:'待命',message:'待命中，等待新的信号'};
}
export const motionEnabled=({reduced=false,hardwareConcurrency=8,deviceMemory=8}={})=>!reduced && hardwareConcurrency>2 && deviceMemory>2;
export const isDrag=(start,current)=>Math.hypot(current.x-start.x,current.y-start.y)>8;
const svg=`<svg viewBox="0 0 120 120" role="img" aria-label="MODI 团队机器人"><ellipse cx="60" cy="110" rx="34" ry="4" fill="#22d3ee" opacity=".15"/><g class="mascot-body"><path d="M60 24V14" stroke="#7de8fa" stroke-width="3"/><circle cx="60" cy="12" r="5" fill="#f5cd74"/><g class="mascot-arms" stroke="#8bd8ea" stroke-width="12" stroke-linecap="round"><path d="M32 78 24 94M88 78 96 94"/></g><rect x="39" y="71" width="42" height="31" rx="14" fill="#2f819d" stroke="#92e8f2"/><path d="M47 103v6m26-6v6" stroke="#a5e3ed" stroke-width="9" stroke-linecap="round"/><rect x="21" y="27" width="78" height="49" rx="22" fill="#89dce9" stroke="#d9ffff"/><rect x="28" y="34" width="64" height="35" rx="15" fill="#102339"/><g class="mascot-eyes" fill="#6ee7ed"><rect x="40" y="43" width="9" height="14" rx="4"/><rect x="71" y="43" width="9" height="14" rx="4"/></g><path class="mascot-mouth" d="M54 60q6 5 12 0" fill="none" stroke="#6ee7ed" stroke-width="2"/><circle cx="60" cy="85" r="5" fill="#f5cd74"/></g><text class="mascot-zzz" x="83" y="23" fill="#f5cd74" font-size="16">Zzz</text><path class="mascot-screen" d="M28 100h64v15H28z m8 11 12-6 12 3 12-5 12 5" fill="#132c43" stroke="#6ee7ed"/></svg>`;
export class Mascot {
  constructor(host,drawer) {
    this.host=host;this.drawer=drawer;this.value={state:'offline',label:'离线',message:'等待快照'};this.variant=false;
    host.innerHTML=`<div class="mascot-stage"><button id="mascot-touch" aria-label="团队吉祥物，点按查看状态与报告" aria-expanded="false">${svg}</button></div><div class="mascot-info"><b>MODI 团队 <span id="mascot-status">—</span></b><p id="mascot-bubble" role="status" hidden></p><small id="mascot-hint">点按互动 · 拖动机器人</small><div class="mascot-actions"><button id="mascot-reports">团队报告</button><button id="mascot-collapse" aria-label="收起吉祥物">收起</button></div></div>`;
    this.button=host.querySelector('#mascot-touch');this.stage=host.querySelector('.mascot-stage');this.bubble=host.querySelector('#mascot-bubble');
    this.position={x:0,y:0};try{const p=JSON.parse(localStorage.getItem('modi-mascot-position'));if(Number.isFinite(p?.x)&&Number.isFinite(p?.y))this.position=p;}catch{}
    this.move(this.position);
    this.button.addEventListener('pointerdown',e=>{if(e.button!==0)return;this.start={x:e.clientX,y:e.clientY};this.origin={...this.position};this.dragged=false;this.button.setPointerCapture(e.pointerId);});
    this.button.addEventListener('pointermove',e=>{if(!this.start)return;this.dragged ||= isDrag(this.start,{x:e.clientX,y:e.clientY});if(this.dragged)this.move({x:this.origin.x+e.clientX-this.start.x,y:this.origin.y+e.clientY-this.start.y});});
    this.button.addEventListener('pointerup',()=>{if(!this.start)return;this.suppressClick=this.dragged;setTimeout(()=>{this.suppressClick=false;},0);this.start=null;if(this.dragged){try{localStorage.setItem('modi-mascot-position',JSON.stringify(this.position));}catch{}}});
    this.button.addEventListener('pointercancel',()=>{this.start=null;this.suppressClick=false;});
    this.button.addEventListener('click',()=>{if(this.suppressClick){this.suppressClick=false;return;}if(host.classList.contains('collapsed')){host.classList.remove('collapsed');return;}const showReports=!this.bubble.hidden;this.variant=!this.variant;host.classList.toggle('expression-alt',this.variant);this.bubble.textContent=this.value.message;this.bubble.hidden=false;this.button.setAttribute('aria-expanded','true');host.querySelector('#mascot-hint').textContent='再点机器人查看团队报告';if(showReports)this.open();});
    host.querySelector('#mascot-collapse').addEventListener('click',()=>{host.classList.add('collapsed');this.bubble.hidden=true;this.button.setAttribute('aria-expanded','false');this.move({x:0,y:0});this.button.focus();});
    host.querySelector('#mascot-reports').addEventListener('click',()=>this.open());
    drawer.querySelector('[data-close]').addEventListener('click',()=>drawer.close());
    drawer.addEventListener('click',e=>{if(e.target===drawer){const r=drawer.getBoundingClientRect();if(e.clientY<r.top || e.clientX<r.left || e.clientX>r.right)drawer.close();}});
    drawer.addEventListener('close',()=>host.querySelector('#mascot-reports').focus());
    const media=matchMedia('(prefers-reduced-motion: reduce)');const motion=()=>host.classList.toggle('still',!motionEnabled({reduced:media.matches,hardwareConcurrency:navigator.hardwareConcurrency??8,deviceMemory:navigator.deviceMemory??8}));media.addEventListener('change',motion);motion();
  }
  move(p){this.position={x:Math.max(-16,Math.min(16,p.x)),y:Math.max(-12,Math.min(12,p.y))};this.button.style.transform=`translate(${this.position.x}px,${this.position.y}px)`;}
  update(snapshot,team){this.value=mascotState(snapshot);this.team=team;this.host.dataset.state=this.value.state;this.host.querySelector('#mascot-status').textContent=this.value.label;if(!this.bubble.hidden)this.bubble.textContent=this.value.message;if(this.drawer.open)this.renderReports();}
  renderReports(){
    const reports=Object.entries(ROLES).map(([role,name])=>{const rows=[this.team?.status?.roles?.[role]?.last_report,...(this.team?.log||[]).filter(r=>r.role===role)].filter(Boolean).sort((a,b)=>stamp(b.ts)-stamp(a.ts));const r=rows[0];return r?`<article><h3>${name} · ${esc(r.title)}</h3><p>${esc(r.summary)}</p><small>${esc(r.ts)} · ${esc(r.grade??'未评级')}</small></article>`:'';}).join('');
    this.drawer.querySelector('#mascot-report-list').innerHTML=reports || '<p class="empty">暂无报告</p>';
  }
  open(){this.renderReports();this.drawer.showModal();}
}

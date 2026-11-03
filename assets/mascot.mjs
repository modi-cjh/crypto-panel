import {ROLES} from './team.mjs';
const stamp=v=>Date.parse(v);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const fmt=(v,d=2)=>Number.isFinite(v)?v.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
const signed=v=>Number.isFinite(v)?(v>0?'+':'')+fmt(v):'—';
const coinName=s=>String(s??'').replace('-USDT-SWAP','');

// The clock is an input, so identical snapshots always produce identical states.
export function mascotState(snapshot) {
  const at=stamp(snapshot?.updated_at ?? snapshot?.generated_at),now=snapshot?.now ?? at;
  const account=(snapshot?.accounts || []).find(a=>['paper_main','live_main'].includes(a.id));
  if(!Number.isFinite(at) || !Number.isFinite(now) || !account || snapshot.dataDisconnected || now-at>(snapshot.offlineMinutes??15)*60000)
    return {state:'offline',label:'离线',message:'快照过期或数据中断，我在努力重连'};
  const pause=snapshot.pause ?? account.pause;
  if(pause?.paused || account.status==='暂停')return {state:'paused',label:'暂停',message:'模拟盘暂停中'};
  const recent=t=>Number.isFinite(stamp(t)) && now-stamp(t)>=0 && now-stamp(t)<=15*60000;
  const events=snapshot.events || [];
  // Public fills carry no close reason, so stop-loss detection relies on events only.
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

// Each state, and each short-lived reaction, maps to one face: eyes, mouth and accessories.
export const FACES={
  idle:{eyes:'normal',mouth:'smile',acc:[]},
  happy:{eyes:'happy',mouth:'grin',acc:['spark']},
  excited:{eyes:'star',mouth:'grin',acc:['spark']},
  alert:{eyes:'wide',mouth:'o',acc:['alert']},
  sad:{eyes:'sad',mouth:'frown',acc:['tear']},
  paused:{eyes:'closed',mouth:'flat',acc:['zzz']},
  offline:{eyes:'dash',mouth:'wavy',acc:['wifi']},
  squash:{eyes:'happy',mouth:'o',acc:[]},
  wave:{eyes:'happy',mouth:'grin',acc:[]},
  dizzy:{eyes:'dizzy',mouth:'wavy',acc:['sweat']},
  love:{eyes:'happy',mouth:'smile',acc:['heart']},
  report:{eyes:'normal',mouth:'smile',acc:['paper']},
};
export function faceFor(state,reaction=null){return FACES[reaction] || FACES[state] || FACES.idle;}

// Snap to the nearest side; vertical position is kept as distance from the bottom so
// a mobile URL bar showing or hiding does not push the companion off-screen.
export function snapPosition({x,y},{width,height},size=72,margin=12,top=72){
  const side=x+size/2<width/2?'left':'right';
  const bottom=Math.min(Math.max(margin,height-y-size),Math.max(margin,height-top-size));
  return {side,bottom:Math.round(bottom)};
}
export function fillLine(fill){
  const coin=coinName(fill.symbol),side=fill.side===1?'多':fill.side===-1?'空':'';
  if(fill.kind==='open')return {text:`开仓 ${coin} ${side} @ ${fill.price}`,mood:'excited'};
  const pnl=Number.isFinite(fill.pnl)?` ${signed(fill.pnl)} USDT`:'';
  return {text:`平仓 ${coin} ${side}${pnl}`.trim(),mood:Number.isFinite(fill.pnl)&&fill.pnl<0?'sad':'happy'};
}
export function eventLine(event){
  const coin=event.coin?' '+coinName(event.coin):'';
  if(event.type==='异动' && event.wake===true)return {text:`发现异动${coin}${event.reason?' · '+event.reason:''}，团队开会研判`,mood:'alert'};
  if(event.type==='daily_halt')return {text:'触发日亏停手，今天不再开新仓',mood:'sad'};
  if(event.type==='liquidated')return {text:`强平${coin}，风控复盘中`,mood:'sad'};
  return null;
}
export const reportLine=row=>({text:`${ROLES[row.role] || '团队'}交了报告：${row.title}${row.grade?' · '+row.grade:''}`,mood:'report'});
export function contextTip(c={}){
  if(c.route==='coin')return `你在看 ${coinName(c.coin)}，${c.decision?'会议结论：'+c.decision:'暂无会议结论'}。`;
  if(c.route==='paper')return '这里是账户历史：最大回撤按所选范围从峰值到之后低点计算。';
  if(c.route==='market')return '点任一币种，可以看均值回归入场条件和 K 线。';
  if(c.route==='sim')return '这里是学习用模拟盘：一个账户、三套策略，每 30 分钟检查一次；持仓浮盈按实时价计算。';
  if(c.sim)return `模拟盘累计 ${signed(c.sim.totals.ret)}%，持仓 ${c.sim.totals.positions} 个；有开平仓我会马上告诉你。`;
  if(c.paused)return '模拟盘暂停中，重新开始后我会第一时间播报开平仓。';
  if(c.positions>0)return `当前 ${c.positions} 个持仓，点持仓行可展开 K 线。`;
  return '暂无持仓，有新信号或成交我会马上告诉你。';
}
// Bounded queue; the same line inside the dedupe window is spoken once.
export function pushMessage(queue,message,{max=5,now=Date.now(),window=10000}={}){
  if(queue.some(m=>m.text===message.text && now-m.at<window))return queue;
  return [...queue,{...message,at:now}].slice(-max);
}
const GREETINGS=['嗨！我是 MODI 团队的小助手','今天也要按纪律交易哦','有新动静我第一时间告诉你','戳我两下我会挥手～'];

const svg=(id)=>`<svg viewBox="0 0 120 120" aria-hidden="true" focusable="false"><defs><clipPath id="m-clip-${id}"><rect x="26" y="30" width="68" height="36" rx="16"/></clipPath></defs><ellipse class="m-shadow" cx="60" cy="114" rx="24" ry="3.5"/><g class="m-float"><g class="m-body"><path class="m-line" d="M60 23V13"/><circle class="m-antenna" cx="60" cy="9" r="5"/><path class="m-arm m-arm-l" d="M38 78q-12 4-15 15"/><path class="m-arm m-arm-r" d="M82 78q12 4 15 15"/><g class="m-feet"><path class="m-leg" d="M50 99v8"/><path class="m-leg" d="M70 99v8"/></g><rect class="m-shell" x="38" y="70" width="44" height="31" rx="14"/><circle class="m-core" cx="60" cy="85" r="4.5"/><g class="m-head"><rect class="m-shell" x="18" y="22" width="84" height="52" rx="24"/><rect class="m-screen" x="26" y="30" width="68" height="36" rx="16"/><g clip-path="url(#m-clip-${id})"><path class="m-shine" d="M30 26h10L22 70H12Z"/></g><circle class="m-cheek" cx="35" cy="59" r="4"/><circle class="m-cheek" cx="85" cy="59" r="4"/><g class="m-eyes"><g class="m-blink"><g class="f f-normal m-ink"><rect x="40" y="40" width="9" height="14" rx="4.5"/><rect x="71" y="40" width="9" height="14" rx="4.5"/></g><g class="f f-happy m-stroke"><path d="M39 50q5.5-8 11 0M70 50q5.5-8 11 0"/></g><g class="f f-closed m-stroke"><path d="M39 48q5.5 4 11 0M70 48q5.5 4 11 0"/></g><g class="f f-dash m-stroke"><path d="M40 47h9M71 47h9"/></g><g class="f f-star m-ink"><path d="M44.5 38.5l2.2 4.7 5.1.6-3.8 3.5 1 5.1-4.5-2.6-4.5 2.6 1-5.1-3.8-3.5 5.1-.6z"/><path d="M75.5 38.5l2.2 4.7 5.1.6-3.8 3.5 1 5.1-4.5-2.6-4.5 2.6 1-5.1-3.8-3.5 5.1-.6z"/></g><g class="f f-wide m-ink"><circle cx="44.5" cy="47" r="6.5"/><circle cx="75.5" cy="47" r="6.5"/></g><g class="f f-sad"><path class="m-stroke" d="M38 41l10 3M82 41l-10 3"/><g class="m-ink"><rect x="41" y="45" width="7" height="9" rx="3.5"/><rect x="72" y="45" width="7" height="9" rx="3.5"/></g></g><g class="f f-dizzy m-stroke"><path d="M44.5 47m-5 0a5 5 0 1 0 5-5a3 3 0 1 0 3 3M75.5 47m-5 0a5 5 0 1 0 5-5a3 3 0 1 0 3 3"/></g></g></g><g class="m-mouth"><path class="f f-smile m-stroke" d="M54 58q6 5 12 0"/><path class="f f-grin m-ink" d="M52 56.5q8 9.5 16 0z"/><path class="f f-flat m-stroke" d="M55 59.5h10"/><path class="f f-frown m-stroke" d="M54 62q6-5 12 0"/><ellipse class="f f-o m-ink" cx="60" cy="59.5" rx="3.4" ry="4"/><path class="f f-wavy m-stroke" d="M53 60q2.3-2.5 4.7 0t4.6 0 4.7 0"/></g></g></g><g class="acc acc-zzz"><text x="86" y="20">Z</text><text x="97" y="11" font-size="9">z</text></g><g class="acc acc-alert"><path d="M104 4v13"/><circle cx="104" cy="23" r="2.6"/></g><path class="acc acc-tear" d="M83 62q3.4 5.5 0 8.6q-3.4-3.1 0-8.6z"/><g class="acc acc-wifi"><path d="M85 14q10-8 20 0M89 18q6-4.5 12 0"/><circle cx="95" cy="22" r="1.8"/><path d="M86 8l18 18"/></g><g class="acc acc-spark"><path d="M14 22l1.6 4 4 1.6-4 1.6-1.6 4-1.6-4-4-1.6 4-1.6z"/><path d="M104 30l1.2 3 3 1.2-3 1.2-1.2 3-1.2-3-3-1.2 3-1.2z"/></g><g class="acc acc-heart"><path d="M96 20c-3-4-9-1-6 4l6 5 6-5c3-5-3-8-6-4z"/><path d="M20 28c-2-2.6-6-.7-4 2.6l4 3.3 4-3.3c2-3.3-2-5.2-4-2.6z"/></g><g class="acc acc-sweat"><path d="M100 26q3.4 5 0 8q-3.4-3 0-8z"/></g><g class="acc acc-paper"><rect x="92" y="78" width="16" height="20" rx="2.5"/><path d="M95 84h10M95 88h10M95 92h7"/></g></g></svg>`;

const TABS=[['status','状态'],['team','团队'],['feed','动态']];
export class Mascot {
  constructor(host,panel,{size=72}={}) {
    this.host=host;this.panel=panel;this.size=size;this.value={state:'offline',label:'离线',message:'等待快照'};
    this.queue=[];this.feed=[];this.context={};this.team=null;this.events=[];this.reaction=null;this.speaking=false;this.greeted=false;this.taps=0;this.tab='status';
    host.classList.add('m-host');
    host.innerHTML=`<p id="mascot-bubble" class="companion-bubble" aria-hidden="true" hidden></p><button id="mascot-touch" class="companion-bot" type="button" aria-label="MODI 团队伙伴：点按打开面板，可拖动" aria-haspopup="dialog" aria-expanded="false">${svg('bot')}<span id="mascot-status" class="companion-chip">—</span></button>`;
    panel.innerHTML=`<header class="companion-head"><div class="companion-face m-host" aria-hidden="true">${svg('panel')}</div><div><h2 id="mascot-drawer-title">MODI 团队伙伴</h2><p id="companion-mood">—</p></div><button data-close type="button" aria-label="关闭面板">关闭</button></header><p id="companion-tip" class="companion-tip"></p><div class="companion-tabs" role="tablist" aria-label="伙伴面板">${TABS.map(([k,n])=>`<button type="button" role="tab" id="companion-tab-${k}" aria-controls="companion-panel-${k}" aria-selected="${k==='status'}" tabindex="${k==='status'?0:-1}">${n}</button>`).join('')}</div><section id="companion-panel-status" class="companion-section" role="tabpanel" aria-labelledby="companion-tab-status"></section><section id="companion-panel-team" class="companion-section" role="tabpanel" aria-labelledby="companion-tab-team" hidden><p id="team-updated" class="muted">等待团队数据</p><div id="mascot-report-list"></div></section><section id="companion-panel-feed" class="companion-section" role="tabpanel" aria-labelledby="companion-tab-feed" hidden><ol id="companion-feed" class="companion-feed"></ol></section><div class="companion-foot"><button id="companion-dock" type="button">收进边缘</button></div>`;
    this.button=host.querySelector('#mascot-touch');this.bubble=host.querySelector('#mascot-bubble');this.face=panel.querySelector('.companion-face');
    this.eyes=host.querySelector('.m-eyes');
    this.restore();this.bindPointer();this.bindPanel();this.bindEyes();
    const media=matchMedia('(prefers-reduced-motion: reduce)');
    const motion=()=>{this.still=!motionEnabled({reduced:media.matches,hardwareConcurrency:navigator.hardwareConcurrency??8,deviceMemory:navigator.deviceMemory??8});host.classList.toggle('still',this.still);this.face.classList.toggle('still',this.still);};
    media.addEventListener('change',motion);motion();
    addEventListener('resize',()=>this.place());
    document.documentElement.classList.add('has-companion');
    // Focus is returned to the robot for keyboard and screen-reader users, but touch input
    // (native gestures are blocked on the robot) makes browsers draw the keyboard ring.
    // Companion focus rings are off by default and only shown once a key is pressed,
    // so a stale cached script can never bring the ring back on phones.
    const root=document.documentElement;
    addEventListener('keydown',()=>root.classList.add('companion-keyboard'),{capture:true});
    addEventListener('pointerdown',()=>root.classList.remove('companion-keyboard'),{capture:true,passive:true});
    this.applyFace();
  }
  // ---- position -----------------------------------------------------------
  restore(){
    let saved=null;try{saved=JSON.parse(localStorage.getItem('modi-companion-position'));}catch{}
    this.pos={side:saved?.side==='left'?'left':'right',bottom:Number.isFinite(saved?.bottom)?saved.bottom:20};
    this.docked=saved?.docked===true;this.place();
  }
  save(){try{localStorage.setItem('modi-companion-position',JSON.stringify({...this.pos,docked:this.docked}));}catch{}}
  place(){
    const w=innerWidth,h=innerHeight,s=this.size,m=12;
    const bottom=Math.min(Math.max(m,this.pos.bottom),Math.max(m,h-72-s));
    this.moveTo(this.pos.side==='left'?m:w-s-m,h-bottom-s);
    this.host.dataset.side=this.pos.side;this.host.classList.toggle('docked',this.docked);
    this.host.classList.toggle('bubble-below',h-bottom-s<150);
  }
  // Position with a compositor-only transform: no layout work while dragging or snapping.
  moveTo(x,y){this.xy={x,y};this.host.style.transform=`translate3d(${x}px,${y}px,0)`;}
  dock(value){this.docked=value;this.place();this.save();}
  // ---- pointer: tap / double tap / triple tap / long press / drag ---------
  bindPointer(){
    const b=this.button;
    b.addEventListener('pointerdown',e=>{
      if(e.button!==0)return;
      const r=this.host.getBoundingClientRect();
      this.drag={start:{x:e.clientX,y:e.clientY},offset:{x:e.clientX-r.left,y:e.clientY-r.top},moved:false};
      b.setPointerCapture?.(e.pointerId);
      clearTimeout(this.pressTimer);
      this.pressTimer=setTimeout(()=>{if(this.drag && !this.drag.moved){this.drag.longPress=true;this.say('被你发现啦，比心 ♥','love',2400,{interrupt:true});}},550);
    });
    b.addEventListener('pointermove',e=>{
      const d=this.drag;if(!d)return;
      d.moved ||= isDrag(d.start,{x:e.clientX,y:e.clientY});
      if(!d.moved)return;
      clearTimeout(this.pressTimer);
      if(!this.host.classList.contains('dragging')){this.host.classList.add('dragging');this.host.classList.remove('snapping','docked');this.docked=false;}
      const s=this.size;
      d.last={x:Math.min(innerWidth-s,Math.max(0,e.clientX-d.offset.x)),y:Math.min(innerHeight-s,Math.max(0,e.clientY-d.offset.y))};
      // Several pointermove events can arrive per frame; paint only the latest once per frame.
      if(!d.frame)d.frame=requestAnimationFrame(()=>{d.frame=0;if(this.drag===d)this.moveTo(d.last.x,d.last.y);});
    });
    const end=cancel=>{
      const d=this.drag;this.drag=null;clearTimeout(this.pressTimer);if(!d)return;
      if(d.frame)cancelAnimationFrame(d.frame);
      this.host.classList.remove('dragging');
      // A cancelled drag (e.g. the OS took the gesture) still snaps instead of leaving the robot mid-screen.
      if(d.moved && d.last){
        this.pos=snapPosition(d.last,{width:innerWidth,height:innerHeight},this.size);
        this.host.classList.add('snapping');this.place();this.save();
        setTimeout(()=>this.host.classList.remove('snapping'),320);
      }
      if(!cancel && !d.moved && !d.longPress)this.tap();
    };
    b.addEventListener('pointerup',()=>end(false));
    b.addEventListener('pointercancel',()=>end(true));
    b.addEventListener('lostpointercapture',()=>{if(this.drag)end(true);});
    // iOS Safari otherwise claims a held touch for its callout / selection / zoom and cancels the drag.
    // Taps are recognised on pointerup, so blocking native touch handling loses nothing.
    for(const type of ['touchstart','touchmove'])b.addEventListener(type,e=>{if(e.cancelable)e.preventDefault();},{passive:false});
    b.addEventListener('contextmenu',e=>e.preventDefault());
    b.addEventListener('click',e=>{if(e.detail===0)this.tap(true);}); // keyboard activation only
  }
  tap(keyboard=false){
    if(this.docked){this.dock(false);this.react('wave',1200);return;}
    if(keyboard){this.open();return;}
    this.taps++;clearTimeout(this.tapTimer);
    this.tapTimer=setTimeout(()=>{
      const n=this.taps;this.taps=0;
      if(n>=3)this.say('别戳啦，头好晕～','dizzy',2400,{interrupt:true});
      else if(n===2)this.say(GREETINGS[Math.floor(Math.random()*GREETINGS.length)],'wave',3000,{interrupt:true});
      else {this.react('squash',450);this.open();}
    },260);
  }
  // ---- eyes follow the pointer; idle glances when nothing moves -------------
  bindEyes(){
    let frame=0,last=0;
    const look=(dx,dy)=>{this.eyes.style.transform=`translate(${dx}px,${dy}px)`;};
    document.addEventListener('pointermove',e=>{
      last=Date.now();if(this.still || frame || this.drag?.moved)return; // no layout reads mid-drag
      frame=requestAnimationFrame(()=>{
        frame=0;const p=this.xy || {x:0,y:0},cx=p.x+this.size/2,cy=p.y+this.size*.4;
        const dx=e.clientX-cx,dy=e.clientY-cy,len=Math.hypot(dx,dy)||1,k=Math.min(1,len/160);
        look(+(dx/len*3.2*k).toFixed(2),+(dy/len*2.4*k).toFixed(2));
      });
    },{passive:true});
    document.addEventListener('pointerleave',()=>look(0,0));
    const glance=()=>{
      this.glanceTimer=setTimeout(()=>{
        if(!this.still && !document.hidden && Date.now()-last>3000 && !['paused','offline'].includes(this.value.state)){
          const dir=Math.random()<.5?-1:1;look(dir*3,Math.random()*2-1);setTimeout(()=>{if(Date.now()-last>3000)look(0,0);},900);
        }
        glance();
      },4000+Math.random()*5000);
    };
    glance();
  }
  // ---- face, reactions and speech ------------------------------------------
  applyFace(){
    const f=faceFor(this.value.state,this.reaction);
    for(const el of [this.host,this.face]){el.dataset.state=this.value.state;el.dataset.eyes=f.eyes;el.dataset.mouth=f.mouth;el.dataset.acc=f.acc.join(' ');el.dataset.reaction=this.reaction || '';}
  }
  react(name,ms=1200){
    clearTimeout(this.reactTimer);this.reaction=name;this.applyFace();
    this.reactTimer=setTimeout(()=>{this.reaction=null;this.applyFace();},ms);
  }
  // Direct interaction interrupts whatever is being said; data narration waits its turn.
  say(text,mood='idle',duration=4500,{interrupt=false}={}){
    if(interrupt){clearTimeout(this.speakTimer);this.queue=[{text,mood,duration,at:Date.now()},...this.queue];this.speaking=false;}
    else this.queue=pushMessage(this.queue,{text,mood,duration});
    this.feed=[{ts:Date.now(),text,mood},...this.feed].slice(0,30);
    if(this.panel.open && this.tab==='feed')this.renderFeed();
    if(!this.speaking)this.next();
  }
  next(){
    const m=this.queue.shift();
    if(!m){this.speaking=false;this.bubble.hidden=true;return;}
    this.speaking=true;this.bubble.textContent=m.text;this.bubble.dataset.mood=m.mood;this.bubble.hidden=false;
    // Re-trigger the entrance animation for consecutive lines.
    this.bubble.style.animation='none';void this.bubble.offsetWidth;this.bubble.style.animation='';
    if(FACES[m.mood])this.react(m.mood,Math.min(m.duration,2400));
    clearTimeout(this.speakTimer);this.speakTimer=setTimeout(()=>this.next(),m.duration);
  }
  update(snapshot,team,context={}){
    const previous=this.value;
    // Paper accounts drive the companion once their data has loaded; the old bot snapshot is the fallback.
    this.value=context.simState || mascotState(snapshot);this.team=team;this.events=snapshot?.events || [];this.context=context;
    this.host.querySelector('#mascot-status').textContent=this.value.label;
    this.applyFace();
    if(this.value.state!==previous.state){
      // A fill or alert line usually explains the change already; avoid saying it twice.
      if(this.greeted && !(this.feed[0] && Date.now()-this.feed[0].ts<5000))this.say(this.value.message,this.value.state);
      else if(this.value.state!=='offline'){this.greeted=true;setTimeout(()=>this.say(`嗨，我是 MODI · ${this.value.message}，点我看团队状态`,this.value.state==='paused'?'idle':this.value.state),900);}
    }
    if(this.panel.open)this.render();
  }
  // ---- panel -----------------------------------------------------------------
  bindPanel(){
    const p=this.panel;
    p.querySelector('[data-close]').addEventListener('click',()=>p.close());
    p.addEventListener('click',e=>{if(e.target===p){const r=p.getBoundingClientRect();if(e.clientY<r.top || e.clientX<r.left || e.clientX>r.right)p.close();}});
    p.addEventListener('close',()=>{this.button.setAttribute('aria-expanded','false');this.button.focus();});
    p.querySelector('#companion-dock').addEventListener('click',()=>{p.close();this.dock(true);});
    const tabs=[...p.querySelectorAll('[role=tab]')];
    p.querySelector('[role=tablist]').addEventListener('click',e=>{const t=e.target.closest('[role=tab]');if(t)this.select(t.id.replace('companion-tab-',''));});
    p.querySelector('[role=tablist]').addEventListener('keydown',e=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();
      const i=tabs.findIndex(t=>t.getAttribute('aria-selected')==='true');
      const n=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
      this.select(tabs[n].id.replace('companion-tab-',''));tabs[n].focus();
    });
  }
  select(tab){
    this.tab=tab;
    for(const [k] of TABS){const t=this.panel.querySelector('#companion-tab-'+k),on=k===tab;t.setAttribute('aria-selected',String(on));t.tabIndex=on?0:-1;this.panel.querySelector('#companion-panel-'+k).hidden=!on;}
    this.render();
  }
  open(){this.render();if(!this.panel.open)this.panel.showModal();this.button.setAttribute('aria-expanded','true');}
  render(){
    this.panel.querySelector('#companion-mood').textContent=this.value.message;
    this.panel.querySelector('#companion-tip').textContent=contextTip(this.context);
    if(this.tab==='status')this.renderStatus();else if(this.tab==='team')this.renderReports();else this.renderFeed();
  }
  renderStatus(){
    const c=this.context,metric=(label,value,cls='')=>`<div><span>${label}</span><strong class="${cls}">${value}</strong></div>`;
    const tone=v=>Number.isFinite(v)?v>=0?'up':'down':'';
    if(c.sim){
      const t=c.sim.totals;
      this.panel.querySelector('#companion-panel-status').innerHTML=`<div class="companion-metrics">${metric('账户权益 USDT',fmt(t.equity))}${metric('累计收益',`${signed(t.ret)}%`,tone(t.ret))}${metric('当前持仓',t.positions+' 仓')}${metric('已平仓',t.trades+' 笔')}</div>`+
        (c.sim.strategies?.length
          ? `<ul class="companion-accounts">${c.sim.strategies.map(b=>`<li><span><i style="background:${esc(b.color)}"></i>${esc(b.name)}</span><span>已平仓 ${b.closed_trades} · <b class="${tone(b.pnl)}">${signed(b.pnl)} U</b> · 持仓 ${b.positions}</span></li>`).join('')}</ul>`
          : `<ul class="companion-accounts">${c.sim.accounts.map(a=>`<li><span><i style="background:${esc(a.color)}"></i>${esc(a.name)}${a.halted?' · <b class="down">停手</b>':''}</span><span>${fmt(a.equity)} <b class="${tone(a.return_pct)}">${signed(a.return_pct)}%</b> · 持仓 ${a.positions}</span></li>`).join('')}</ul>`);
      return;
    }
    const bar=(label,r)=>{const used=r && Number.isFinite(r.ratio) && r.limit>0?Math.min(100,Math.max(0,r.ratio/r.limit*100)):null;
      return `<div class="companion-bar"><div><span>${label}</span><b class="${used>=100?'down':''}">${used===null?'—':fmt(r.ratio*100)+'%'} / ${r?.limit?fmt(r.limit*100,0)+'%':'—'}</b></div><progress max="100" value="${used??0}" aria-label="${label}" ${used===null?'data-missing="true"':''}></progress></div>`;};
    this.panel.querySelector('#companion-panel-status').innerHTML=`<div class="companion-metrics">${metric('账户权益 USDT',fmt(c.equity))}${metric('今日盈亏',Number.isFinite(c.daily)?`${signed(c.daily)} / ${signed(c.percent)}%`:'—',tone(c.daily))}${metric('当前持仓',c.paused?'暂停中':Number.isFinite(c.positions)?c.positions+' 仓':'—')}${metric('数据来源',esc(c.source || '—'))}</div>${bar('当日亏损 · 停手线',c.loss)}${bar('多头合计风险',c.long)}${bar('空头合计风险',c.short)}`;
  }
  renderReports(){
    const reports=Object.entries(ROLES).map(([role,name])=>{const rows=[this.team?.status?.roles?.[role]?.last_report,...(this.team?.log||[]).filter(r=>r.role===role)].filter(Boolean).sort((a,b)=>stamp(b.ts)-stamp(a.ts));const r=rows[0];return r?`<article><h3>${name} · ${esc(r.title)}</h3><p>${esc(r.summary)}</p><small>${esc(r.ts)} · ${esc(r.grade??'未评级')}</small></article>`:'';}).join('');
    this.panel.querySelector('#mascot-report-list').innerHTML=reports || '<p class="empty">暂无报告</p>';
  }
  renderFeed(){
    const clock=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Bangkok',hour:'2-digit',minute:'2-digit',hour12:false});
    const said=this.feed.map(m=>({ts:m.ts,text:m.text,mood:m.mood}));
    const tidy=t=>String(t).replace(/-?\d+\.\d{5,}/g,n=>String(+Number(n).toPrecision(4)));
    const events=this.context.simFeed ? this.context.simFeed.map(r=>({...r,mood:r.mood==='sad'?'sad':'event'}))
      : (this.events||[]).slice(-12).reverse().map(e=>({ts:stamp(e.ts),text:tidy(this.context.eventText?.(e) || e.type),mood:'event'}));
    const rows=[...said,...events].filter(r=>Number.isFinite(r.ts)).sort((a,b)=>b.ts-a.ts).slice(0,30);
    this.panel.querySelector('#companion-feed').innerHTML=rows.length?rows.map(r=>`<li data-mood="${r.mood}"><time>${clock.format(r.ts)}</time><span>${esc(r.text)}</span></li>`).join(''):'<li class="empty">还没有动态，有新成交、异动或报告时我会记在这里</li>';
  }
}

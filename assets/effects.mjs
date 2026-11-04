import { animationClasses, motionAllowed } from './state.mjs';
const effectTimers=new WeakMap();
export const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
export function configureMotion(){
 const root=document.documentElement;
 const update=()=>{
  const reduced=reducedMotion(),hidden=document.hidden;
  root.classList.toggle('reduced-motion',reduced);root.classList.toggle('page-hidden',hidden);
  if(reduced || hidden){
   document.querySelectorAll('.report-particle').forEach(node=>node.remove());
   document.querySelectorAll('.number-in').forEach(node=>node.replaceWith(document.createTextNode(node.textContent)));
   document.querySelectorAll('.typing-line').forEach(node=>{node.textContent=node.dataset.fullText || node.textContent;node.classList.remove('typing-line');delete node.dataset.fullText;});
  }
  document.querySelectorAll('[data-role]').forEach(card=>{
   card.classList.remove('motion-idle','motion-working','motion-done');
   const state=['offline','working','idle','done'].find(s=>card.classList.contains('state-'+s));
   const cls=animationClasses(state,{reduced,hidden});if(cls)card.classList.add(cls);
   if(reduced || hidden)card.classList.remove('effect-report','effect-excited','effect-warning');
  });
 };
 root.classList.toggle('low-power',(navigator.hardwareConcurrency || 8)<=4 || (navigator.deviceMemory || 8)<=4 || !CSS.supports('backdrop-filter','blur(2px)'));
 matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change',update);
 document.addEventListener('visibilitychange',update);update();
}
export function toast(message){
 const host=document.getElementById('toasts');if(!host)return;
 const node=document.createElement('div');node.className='toast';node.textContent=message;host.append(node);
 setTimeout(()=>node.remove(),6500);
}
export function roleEffect(role,kind,label='止损'){
 const card=document.querySelector(`[data-role="${role}"]`);
 if(!card || !motionAllowed({reduced:reducedMotion(),hidden:document.hidden,state:card.classList.contains('state-offline')?'offline':'idle'}))return;
 card.querySelector('.warning-label').textContent=label;
 card.classList.remove('effect-'+kind);void card.offsetWidth;card.classList.add('effect-'+kind);
 const timers=effectTimers.get(card)||new Map();clearTimeout(timers.get(kind));
 timers.set(kind,setTimeout(()=>{card.classList.remove('effect-'+kind);timers.delete(kind);},2600));effectTimers.set(card,timers);
 if(kind==='report'){
  const target=document.querySelector('[data-role="umi"]');if(!target)return;
  const a=card.getBoundingClientRect(),b=target.getBoundingClientRect();
  const dot=document.createElement('span');dot.className='report-particle';dot.textContent='▤';
  dot.style.cssText=`left:${a.left+a.width/2}px;top:${a.top+70}px;--dx:${b.left+b.width/2-a.left-a.width/2}px;--dy:${b.top-a.top}px;--role-color:${getComputedStyle(card).getPropertyValue('--role-color')}`;
  document.body.append(dot);setTimeout(()=>dot.remove(),1800);
 }
}
// minStep: changes smaller than this update the text quietly instead of playing the roll animation.
// Gentle number updates: only the digits that changed fade in with a slight rise and a short green / red tint;
// the rest of the number stays perfectly still, so frequent small price moves read as calm. Live prices tick many
// times a second, so one element animates at most once per ROLL_GAP; updates in between change the digits quietly.
const ROLL_GAP=3000;
export function rollNumber(element,value,minStep=0){
 if(!element)return;
 if(!Number.isFinite(value)){element.textContent='—';delete element.dataset.value;return;}
 const text=value.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}),old=Number(element.dataset.value);
 const changed=element.dataset.value!==undefined && Math.round(old*100)!==Math.round(value*100) && Math.abs(value-old)>=minStep;
 element.dataset.value=value;
 if(!changed){if(element.textContent!==text)element.textContent=text;return;}
 if(reducedMotion() || document.hidden || Date.now()-(element.__rolledAt||0)<ROLL_GAP){if(element.textContent!==text)element.textContent=text;return;}
 element.__rolledAt=Date.now();
 const prev=element.textContent;let i=0;
 while(i<prev.length && i<text.length && prev[i]===text[i])i++;
 const incoming=document.createElement('span');incoming.className=`number-in ${value>old?'number-up':'number-down'}`;incoming.textContent=text.slice(i);
 element.replaceChildren(document.createTextNode(text.slice(0,i)),incoming);
 setTimeout(()=>{if(element.dataset.value===String(value))element.textContent=text;},1300);
}
// Per-node timers stop when the node is removed; hidden/reduced pages finish directly.
export function typeLine(node,text){
 if(reducedMotion() || document.hidden){node.textContent=text;return;}
 node.dataset.fullText=text;node.classList.add('typing-line');let i=0;const chars=Array.from(text);
 const step=()=>{if(!node.isConnected)return;if(reducedMotion() || document.hidden)i=chars.length;else i+=1;node.textContent=chars.slice(0,i).join('');if(i<chars.length)setTimeout(step,18);else {node.classList.remove('typing-line');delete node.dataset.fullText;}};step();
}

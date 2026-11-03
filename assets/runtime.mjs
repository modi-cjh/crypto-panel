const unavailable=()=>({mode:'unavailable',control_url:null,account_url:null,health_url:null});
export function normalizeRuntimeConfig(value, base){
 if(value?.mode==='github_actions')return {mode:'github_actions',control_url:null,account_url:null,health_url:null};
 if(value?.mode!=='persistent')return unavailable();
 const result={mode:'persistent'};
 for(const key of ['control_url','account_url','health_url']){
  if(typeof value[key]!=='string'||!value[key])return unavailable();
  try{const u=new URL(value[key],base),b=new URL(base);if(u.username||u.password||!(u.protocol==='https:'||(u.origin===b.origin&&u.protocol==='http:'&&['127.0.0.1','localhost'].includes(u.hostname))))return unavailable();result[key]=u.href;}
  catch{return unavailable();}
 }
 return result;
}
let config;
export function runtimeConfig(){
 if(!config){const url=new URL('../runtime-config.json',import.meta.url);config=fetch(url,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(5000)}).then(async r=>{if(!r.ok)throw new Error();return normalizeRuntimeConfig(await r.json(),url);}).catch(unavailable);}
 return config;
}
export function runtimeHealthText(config,health){
 if(config.mode==='github_actions')return '执行调度：GitHub 定时任务，可能延迟；账户数据以更新时间为准。';
 if(config.mode==='unavailable')return '运行环境配置无法读取，请刷新后重试。';
 if(!health)return '常驻服务暂时不可达，无法确认执行状态。';
 if(!health.enabled)return '常驻服务已连接，交易执行未启用。';
 if(!health.worker_alive||health.heartbeat_age_seconds===null||health.heartbeat_age_seconds>10)return '常驻执行器心跳异常，请到控制台核对状态。';
 return `常驻执行器心跳正常 · ${health.paused?'新开仓已暂停':'按策略执行'}${health.last_success?' · 最近成功 '+new Date(health.last_success*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Bangkok',hour12:false})+' UTC+7':' · 等待首轮成功回执'}`;
}
export async function refreshRuntimeHealth(){
 const config=await runtimeConfig();let health=null;
 if(config.mode==='persistent'){
  try{const r=await fetch(config.health_url,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(3000)});if(r.ok)health=await r.json();}catch{/* Show uncertainty instead of stale health. */}
 }
 const text=runtimeHealthText(config,health);
 document.querySelectorAll('[data-runtime-health]').forEach(el=>{el.textContent=text;});
}

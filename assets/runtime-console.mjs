const $=id=>document.getElementById(id);
let csrf='',busy=false,pending=false;
const labels={queued:'已接收，等待执行',running:'正在执行，尚未确认完成',completed:'执行完成',failed:'执行失败，保持暂停'};
const clock=t=>t?new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Bangkok',hour12:false})+' UTC+7':'—';
async function api(path,method='GET',data={},id){
 const response=await fetch(path,{method,credentials:'same-origin',cache:'no-store',headers:method==='GET'?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf,...(id?{'Idempotency-Key':id}:{})},...(method==='GET'?{}:{body:JSON.stringify(data)})});
 const body=await response.json();
 if(!response.ok){if(response.status===401){csrf='';$('login').hidden=false;$('controls').hidden=true;}throw new Error(typeof body.detail==='string'?body.detail:'请求失败，请核对回执');}
 return body;
}
async function refresh(){
 if(!csrf||busy)return;
 busy=true;
 try{
  const s=await api('/api/status');
  $('service-status').textContent=s.enabled?'常驻模拟执行器已启用':'部署预备状态：交易执行未启用';
  $('heartbeat').textContent=`心跳：${s.worker_alive&&s.heartbeat_age_seconds!==null&&s.heartbeat_age_seconds<10?'正常':'异常 / 等待启动'} · 最近成功运行 ${clock(s.last_success)}`;
  $('cycle').textContent=s.cycle?`本轮任务：${s.cycle.stage} / ${s.cycle.state}`:'尚未执行交易任务';
  $('account-state').textContent=(s.paused?'新开仓：已暂停':'新开仓：按策略执行')+(s.last_error?' · '+s.last_error:'');
  pending=Boolean(s.command&&['queued','running'].includes(s.command.state));
  $('stop-button').disabled=!s.enabled||pending;$('resume-button').disabled=!s.enabled||pending;
  $('receipt').textContent=s.command?`${s.command.action==='stop'?'全部平仓并停止':'恢复交易'} · ${labels[s.command.state]} · ${s.command.message||''} · ${clock(s.command.updated)}`:'暂无控制请求';
 }catch(e){$('message').textContent=e.message;}finally{busy=false;}
}
async function login(){
 $('login-button').disabled=true;
 try{const s=await api('/api/login','POST',{username:$('username').value,password:$('password').value});csrf=s.csrf;$('password').value='';$('login').hidden=true;$('controls').hidden=false;$('message').textContent='';await refresh();}
 catch(e){$('message').textContent=e.message;}finally{$('login-button').disabled=false;}
}
async function control(action){
 if(pending)return;
 if(!confirm(action==='stop'?'确认关闭本程序管理的全部模拟仓位，并暂停后续新开仓？':'确认恢复按策略交易？日亏停手仍然有效。'))return;
 pending=true;$('stop-button').disabled=true;$('resume-button').disabled=true;
 const id=crypto.randomUUID();
 try{const c=await api('/api/control/'+action,'POST',{},id);$('message').textContent=`请求已接收，编号 ${c.id}；正在等待执行回执。`;}
 catch(e){$('message').textContent=e.message+'。请先核对最新回执，避免重复操作。';}
 finally{pending=false;await refresh();}
}
$('login-button').addEventListener('click',login);
$('password').addEventListener('keydown',e=>{if(e.key==='Enter')login();});
$('stop-button').addEventListener('click',()=>control('stop'));
$('resume-button').addEventListener('click',()=>control('resume'));
$('logout-button').addEventListener('click',async()=>{try{await api('/api/logout','POST');}finally{csrf='';$('login').hidden=false;$('controls').hidden=true;}});
try{const s=await api('/api/session');csrf=s.csrf;$('login').hidden=true;$('controls').hidden=false;await refresh();}catch{/* First visit requires login. */}
setInterval(()=>{if(!document.hidden)refresh();},2000);

import { mainAccounts } from './account.mjs';
// OKX public read-only transport. Open sockets are not evidence of live data.
export const backoffDelay = failures => Math.min(30000,1000*2**Math.max(0,failures-1));
export class PublicStream {
  constructor({url,args,fallbackUrls=[],connectTimeout=12000,onData=()=>{},onState=()=>{},maxFailures=4,WebSocketImpl=globalThis.WebSocket,now=()=>Date.now()}){
    Object.assign(this,{url,args,fallbackUrls,connectTimeout,onData,onState,maxFailures,WebSocketImpl,now});
    this.endpointIndex=0;
    this.failures=0;this.lastData=0;this.state='离线';this.stopped=true;this.timers=new Set();
  }
  stateChange(state,reason){this.state=state;this.onState(state,reason);}
  later(fn,ms){const id=setTimeout(()=>{this.timers.delete(id);fn();},ms);this.timers.add(id);return id;}
  start(){this.stop();this.stopped=false;this.failures=0;this.endpointIndex=0;this.connect();}
  connect(){
    if(this.stopped)return;
    this.stateChange('离线',this.failures?'连接中断，指数退避重连':'等待首条实时数据');
    let socket;
    this.activeUrl=[this.url,...this.fallbackUrls][this.endpointIndex];
    try {socket=new this.WebSocketImpl(this.activeUrl);this.socket=socket;}catch{this.failed();return;}
    let received=this.now(),payloadAt=this.now();
    const timeout=this.later(()=>socket.close(),this.connectTimeout);
    socket.onopen=()=>{socket.send(JSON.stringify({op:'subscribe',args:this.args}));heartbeat();};
    const heartbeat=()=>this.later(()=>{
      if(this.stopped || socket!==this.socket)return;
      if(this.now()-received>40000 || this.now()-payloadAt>45000){socket.close();return;}
      if(socket.readyState===1)socket.send('ping');heartbeat();
    },15000);
    socket.onmessage=e=>{
      received=this.now();if(e.data==='pong')return;
      let body;try{body=JSON.parse(e.data);}catch{return;}
      if(body.event==='error'){socket.close();return;}
      if(!Array.isArray(body.data) || !body.data.length)return;
      // onData rejects malformed / old / mismatched packets before status is live.
      if(this.onData(body)===false)return;
      clearTimeout(timeout);this.timers.delete(timeout);payloadAt=this.now();
      this.failures=0;this.lastData=this.now();this.stateChange('实时','OKX 公共 WebSocket');
    };
    socket.onerror=()=>{socket.close();};
    socket.onclose=()=>{if(!this.stopped && socket===this.socket)this.failed();};
  }
  failed(){
    if(this.stopped)return;
    for(const timer of this.timers)clearTimeout(timer);this.timers.clear();
    this.failures++;
    this.endpointIndex=(this.endpointIndex+1)%(1+this.fallbackUrls.length);
    if(this.failures>=this.maxFailures){
      this.stateChange('轮询','实时连接连续失败，回退 REST；60 秒后重新尝试 WebSocket');
      this.later(()=>{this.failures=0;this.endpointIndex=0;this.connect();},60000);return;
    }
    this.stateChange('离线','连接中断，指数退避重连');this.later(()=>this.connect(),backoffDelay(this.failures));
  }
  stop(){this.stopped=true;for(const timer of this.timers)clearTimeout(timer);this.timers.clear();if(this.socket){if(this.socket.readyState===1)this.socket.send(JSON.stringify({op:'unsubscribe',args:this.args}));this.socket.onclose=null;this.socket.onmessage=null;this.socket.onopen=null;this.socket.close();this.socket=null;}}
}
export function mergeCandle(bars,bar){
  if(!bar || !Number.isFinite(bar.time))return bars;
  if(!bars.length)return [bar];
  if(bar.time<bars.at(-1).time)return bars;
  return bar.time===bars.at(-1).time?[...bars.slice(0,-1),bar]:[...bars,bar].slice(-300);
}
export function tradeMarkers(account,symbol,bars){
  if(!bars.length)return [];
  const rows=mainAccounts(account).flatMap(a=>[
    ...(a.fills || []).filter(f=>f.symbol===symbol),
    ...a.positions.filter(p=>p.symbol===symbol && p.entry_time && !(a.fills || []).some(f=>f.symbol===symbol && f.kind==='open' && Math.abs(Date.parse(f.timestamp)-Date.parse(p.entry_time))<1000)).map(p=>({...p,timestamp:p.entry_time,kind:'open'}))
  ]);
  const seen=new Set();
  return rows.sort((a,b)=>Date.parse(a.timestamp)-Date.parse(b.timestamp)).flatMap(f=>{
    const ts=Date.parse(f.timestamp)/1000;
    if(ts<bars[0].time)return [];
    const bar=[...bars].reverse().find(b=>b.time<=ts);if(!bar)return [];
    const key=[bar.time,f.kind,f.side].join('/');if(seen.has(key))return [];seen.add(key);
    return [{time:bar.time,position:f.kind==='open'?'belowBar':'aboveBar',color:f.kind==='open'?'#06727d':'#c53045',shape:f.kind==='open'?'arrowUp':'arrowDown',text:`${f.kind==='open'?'开仓':'平仓'}${f.side===1?'多':f.side===-1?'空':''}`}];
  });
}

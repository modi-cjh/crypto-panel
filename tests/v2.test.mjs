import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {isStale,roleState,NewRecords,animationClasses,validEvents,eventText} from '../assets/state.mjs';
import {isolatedPnL,liveBalance} from '../assets/account.mjs';
import {teamCards,robotSVG,ROLES} from '../assets/team.mjs';
import {PublicStream,backoffDelay,mergeCandle,tradeMarkers} from '../assets/realtime.mjs';
const now=Date.parse('2026-11-02T12:00:00Z'),ts=new Date(now).toISOString();
const team={schema_version:1,generated_at:ts,status:{updated_at:ts,roles:Object.fromEntries(Object.keys(ROLES).map(role=>[role,{status:'工作中',task:'真实任务',last_report:null}]))},log:[]};
const p={symbol:'BTC-USDT-SWAP',side:1,qty:2,entry:100,margin:25,mark_price:98};
const account=(positions=[p])=>({id:'paper_main',status:'运行中',balance:100,equity:96,day:'2026-11-02',day_start:100,positions});
const data=(accounts=[account()])=>({accounts});
test('只有账户快照超时才离线，默认 15 分钟与配置边界',()=>{
 assert.equal(isStale(ts,now+900000),false);assert.equal(isStale(ts,now+900001),true);
 assert.equal(isStale(null,now),true);assert.equal(isStale(new Date(now+60001).toISOString(),now),true);
 const freshTeam={...team,generated_at:null,status:{...team.status,updated_at:null}};
 for(const d of [team,freshTeam,null]) {
  assert.notEqual(roleState(d,'tech',{now:now+900000,snapshotAt:ts}).state,'offline');
  const resolved=roleState(d,'tech',{now:now+900001,snapshotAt:ts});
  assert.equal(resolved.state,'offline');assert.equal(resolved.reason,'快照超过 15 分钟未更新');
  assert.notEqual(roleState(d,'tech',{now:now+60000,snapshotAt:ts,offlineMinutes:1}).state,'offline');
  assert.equal(roleState(d,'tech',{now:now+60001,snapshotAt:ts,offlineMinutes:1}).state,'offline');
 }
 for(const snapshotAt of [null,undefined,'invalid',new Date(now+60001).toISOString()])
  assert.equal(roleState(team,'tech',{now,snapshotAt}).state,'idle');
});
test('快照新鲜时，过期、缺失和显式离线的团队状态统一待命',()=>{
 const options={now,snapshotAt:ts};
 for(const status of ['工作中','已完成','待命','离线']) {
  const d=structuredClone(team);for(const r of Object.values(d.status.roles))r.status=status;
  assert.equal(roleState(d,'tech',options).state,({'工作中':'working','已完成':'done','待命':'idle','离线':'idle'})[status]);
  for(const timestamp of ['2026-11-02T11:44:59Z',null,'invalid','2026-11-02T12:01:01Z']) for(const field of ['generated_at','updated_at']) {
   const modified=structuredClone(d);
   if(field==='generated_at')modified.generated_at=timestamp;else modified.status.updated_at=timestamp;
   for(const role of Object.keys(ROLES))assert.equal(roleState(modified,role,options).state,'idle');
   const html=teamCards(modified,{},options);
   assert.equal((html.match(/state-idle motion-idle/g)||[]).length,6);
   assert.doesNotMatch(html,/state-offline|motion-working|motion-done|团队状态或快照|分钟未更新/);
  }
 }
 assert.deepEqual(roleState(team,'unknown',options),{status:'待命',state:'idle',reason:'暂无角色状态'});
 const boundary={...team,generated_at:'2026-11-02T11:45:00Z',status:{...team.status,updated_at:'2026-11-02T11:45:00Z'}};
 assert.equal(roleState(boundary,'tech',options).state,'working');
 assert.equal(roleState(boundary,'tech',{...options,now:now+1}).state,'idle');
});
test('新增记录：首载静默、重排/删除/旧记录回流静默、重复条数和同秒新增',()=>{
 const detector=new NewRecords(),a={ts,id:'a',role:'tech'},b={ts,id:'b',role:'risk'};
 assert.deepEqual(detector.update([a]),[]);assert.deepEqual(detector.update([a]),[]);
 assert.deepEqual(detector.update([b,a]),[b]);assert.deepEqual(detector.update([a,b]),[]);
 detector.update([]);assert.deepEqual(detector.update([a]),[]);
 const c={ts:new Date(now+1000).toISOString(),role:'news'};assert.deepEqual(detector.update([c]),[c]);
 assert.deepEqual(detector.update([{...b,id:'older'}]),[]);
 const anonymous=new NewRecords();anonymous.update([c]);assert.deepEqual(anonymous.update([c,c]),[c]);assert.deepEqual(anonymous.update([c,c]),[]);
});
test('reduced-motion/隐藏/离线不生成动画类，六个 SVG 无像素与外部素材',()=>{
 const options={now,snapshotAt:ts,reduced:true};const html=teamCards(team,{},options);
 assert.doesNotMatch(html,/motion-working|motion-idle|effect-/);
 assert.equal(animationClasses('idle',{reduced:true}),'');assert.equal(animationClasses('working',{hidden:true}),'');assert.equal(animationClasses('offline'),'');
 assert.match(teamCards(team,{}, {now,snapshotAt:ts}),/motion-working/);
 for(const role of Object.keys(ROLES)){assert.match(robotSVG(role),/class="robot"/);assert.doesNotMatch(robotSVG(role),/crispEdges|<image|https?:/);}
 const css=readFileSync(new URL('../assets/style.css',import.meta.url),'utf8');assert.match(css,/@media\(prefers-reduced-motion:reduce\)/);assert.match(css,/animation:none!important/);assert.doesNotMatch(css,/Press Start|pixel|crispEdges/);
});
test('浮盈多空、逐仓保证金封顶、每次报价逐仓相加、空仓快照权益',()=>{
 assert.equal(isolatedPnL(p,110),20);assert.equal(isolatedPnL({...p,side:-1},90),20);
 assert.equal(isolatedPnL(p,1),-25);assert.equal(isolatedPnL({...p,side:-1},200),-25);
 assert.equal(isolatedPnL({...p,margin:null},1),-198);assert.equal(isolatedPnL(p,null),null);
 const q={...p,symbol:'ETH-USDT-SWAP',side:-1,qty:3,entry:50,margin:10,mark_price:48};
 const quotes=new Map([[p.symbol,{price:105,at:now}],[q.symbol,{price:45,at:now}]]);
 const result=liveBalance(data([account([p,q]),{...account([]),id:'paper_meeting',balance:50,equity:53,day_start:50}]),quotes,now);
 assert.equal(result.equity,125);assert.equal(result.daily,25);assert.equal(result.percent,25);assert.equal(result.source,'OKX 实时报价');
 assert.equal(liveBalance(data([{...account(),id:'paper_meeting'}]),quotes,now),null);
 quotes.set(p.symbol,{price:1,at:now});assert.equal(liveBalance(data([account([p,q])]),quotes,now).equity,90);
 const empty=liveBalance(data([account([])]),quotes,now);assert.equal(empty.equity,96);assert.equal(empty.source,'空仓 · 快照权益');
});
test('实时价过期/缺价按每仓标记价回退，基准过期不伪造当日盈亏',()=>{
 const q={...p,symbol:'ETH-USDT-SWAP',side:-1,qty:1,entry:50,mark_price:48};
 const quotes=new Map([[p.symbol,{price:105,at:now}],[q.symbol,{price:1,at:now-45001}]]);
 const result=liveBalance(data([account([p,q])]),quotes,now);assert.equal(result.equity,112);assert.equal(result.fallback,true);
 assert.equal(liveBalance(data([{...account([p]),positions:[{...p,mark_price:null}]}]),new Map(),now).equity,96);
 assert.equal(liveBalance(data([{...account(),day:'2026-11-01'}]),quotes,now).daily,null);
 assert.equal(liveBalance(data([{...account(),day_start:null}]),quotes,now).percent,null);
 assert.equal(liveBalance(data([{status:'未启动'}]),quotes,now),null);
});
test('安全事件契约和中文短句；拒绝额外字段和非法价格',()=>{
 const event={ts,type:'trade_opened',coin:p.symbol,strategy:'breakout',direction:'空',price:100,reason:null};
 const wrap=r=>({schema_version:1,events:[r]});assert.equal(validEvents(wrap(event)),true);
 assert.match(eventText(event),/开仓 · BTC · 突破放量 · 空 · @ 100/);
 assert.equal(validEvents(wrap({...event,path:'/private'})),false);assert.equal(validEvents(wrap({...event,price:-1})),false);assert.equal(validEvents(wrap({...event,type:'unknown'})),false);
 const alert={...event,type:'异动',strategy:null,direction:null,price:null,reason:'放量 · 2.5 倍',source_url:'https://www.okx.com/markets'};
 assert.equal(validEvents(wrap(alert)),true);assert.match(eventText(alert),/^异动 · BTC · 放量/);
 for(const source_url of ['http://www.okx.com/','javascript:alert(1)','https://127.0.0.1/','https://10.0.0.1/','https://localhost/','https://foo.local/','https://user:pass@www.okx.com/'])assert.equal(validEvents(wrap({...alert,source_url})),false);
});
test('WS：指数退避、连续失败回退、有效数据才标实时、清理心跳和重连',()=>{
 assert.deepEqual([1,2,3,4,9].map(backoffDelay),[1000,2000,4000,8000,30000]);
 class Socket{constructor(url){this.url=url;this.readyState=1;Socket.last=this;}send(value){this.sent=value;}close(){this.closed=true;this.onclose?.();}}
 const stream=new PublicStream({url:'wss://test',args:[{channel:'candle1H',instId:p.symbol}],WebSocketImpl:Socket,now:()=>now,onData:b=>b.data[0]==='valid',maxFailures:2});
 stream.start();let socket=Socket.last;socket.onopen();assert.equal(stream.state,'离线');assert.match(socket.sent,/subscribe/);
 socket.onmessage({data:JSON.stringify({data:['invalid']})});assert.equal(stream.state,'离线');
 socket.onmessage({data:JSON.stringify({data:['valid']})});assert.equal(stream.state,'实时');
 stream.stop();assert.equal(stream.timers.size,0);assert.equal(socket.closed,true);assert.match(socket.sent,/unsubscribe/);assert.equal(socket.onmessage,null);
 stream.stopped=false;stream.later=(fn,ms)=>{stream.pending={fn,ms};};stream.failed();assert.equal(stream.pending.ms,1000);stream.failed();assert.equal(stream.state,'轮询');assert.equal(stream.pending.ms,60000);
 stream.pending.fn();assert.notEqual(Socket.last,socket);assert.equal(stream.failures,0);
 Socket.last.onmessage({data:JSON.stringify({data:['valid']})});assert.equal(stream.state,'实时');stream.stop();
});
test('实时 K 线只替换最后一根或追加；成交/持仓标记映射到对应 K 线',()=>{
 const bars=[{time:100,close:1},{time:200,close:2}];assert.equal(mergeCandle(bars,{time:50}),bars);
 assert.equal(mergeCandle(bars,{time:200,close:3}).at(-1).close,3);assert.equal(mergeCandle(bars,{time:300}).length,3);
 const timestamp=new Date(210000).toISOString();const d={accounts:[{id:'paper_main',positions:[{...p,entry_time:timestamp}],fills:[{symbol:p.symbol,kind:'open',side:1,timestamp}]}]};
 const markers=tradeMarkers(d,p.symbol,bars);assert.equal(markers.length,1);assert.equal(markers[0].time,200);assert.match(markers[0].text,/开仓多/);
});

test('demo WebSocket starts on 443 and can recover through a separately allowed fallback',()=>{
 const urls=[];class Socket{constructor(url){urls.push(url);this.readyState=1;Socket.last=this;}send(){}close(){this.onclose?.();}}
 const primary='wss://wspap.okx.com/ws/v5/public',backup='wss://wspap.okx.com:8443/ws/v5/public';
 const stream=new PublicStream({url:primary,fallbackUrls:[backup],args:[],WebSocketImpl:Socket,now:()=>now,onData:()=>true});
 stream.later=(fn,ms)=>{stream.pending={fn,ms};};stream.start();assert.equal(urls[0],primary);
 Socket.last.onclose();assert.equal(stream.pending.ms,1000);stream.pending.fn();assert.equal(urls[1],backup);
 Socket.last.onmessage({data:JSON.stringify({data:[{markPx:'1.27'}]})});assert.equal(stream.state,'实时');assert.equal(stream.activeUrl,backup);stream.stop();
});

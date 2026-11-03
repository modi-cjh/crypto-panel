import test from 'node:test';
import assert from 'node:assert/strict';
import {mascotState,isDrag,motionEnabled,faceFor,FACES,snapPosition,fillLine,eventLine,reportLine,contextTip,pushMessage} from '../assets/mascot.mjs';
import {validEvents} from '../assets/state.mjs';
import {validAccount,strategyState} from '../assets/account.mjs';
import {validSnapshot} from '../assets/dashboard.mjs';
import {readFileSync} from 'node:fs';
const now=Date.parse('2026-11-02T19:00Z'),ts=new Date(now).toISOString();
const base={now,updated_at:ts,accounts:[{id:'paper_main',status:'运行中',positions:[],fills:[],daily_pnl:1,day:'2026-11-03'}]};
const state=s=>mascotState(s).state;
test('mascotState is deterministic and obeys offline > paused > stop > open > wake > profit > idle',()=>{
 const s=structuredClone(base);s.accounts[0].daily_pnl=null;
 assert.equal(state(s),'idle');assert.deepEqual(mascotState(s),mascotState(structuredClone(s)));
 s.accounts[0].daily_pnl=1;assert.equal(state(s),'happy');assert.match(mascotState(s).message,/\+1U/);
 s.events=[{ts,type:'异动',wake:true}];assert.equal(state(s),'alert');assert.match(mascotState(s).message,/1 条/);
 s.accounts[0].fills=[{timestamp:ts,kind:'open'}];assert.equal(state(s),'excited');
 s.events.push({ts,type:'trade_closed',reason:'止损'});assert.equal(state(s),'sad');
 s.pause={paused:true};assert.equal(state(s),'paused');
 s.updated_at='2026-11-02T18:44:59Z';assert.equal(state(s),'offline');
 assert.equal(state({...base,dataDisconnected:true}),'offline');assert.equal(state(null),'offline');assert.equal(state({...base,now:NaN}),'offline');
});
test('recent and UTC+7 boundaries; missing values never invent numbers',()=>{
 const s=structuredClone(base);s.accounts[0].day='2026-11-02';s.events=[{ts:'2026-11-02T18:00Z',type:'trade_closed',reason:'止损'},{ts:'2026-11-02T19:01Z',type:'异动',wake:true}];
 assert.equal(state(s),'idle');assert.doesNotMatch(mascotState(s).message,/\d/);
 s.events=[{ts,type:'异动',wake:false}];assert.equal(state(s),'idle');
 s.events=[{ts:'2026-11-02T18:45Z',type:'异动',wake:true}];assert.equal(state(s),'alert');
 assert.equal(state({...s,updated_at:'2026-11-02T18:45Z'}),'alert');
});
test('drag threshold, low device and reduced motion',()=>{
 assert.equal(isDrag({x:0,y:0},{x:4,y:4}),false);assert.equal(isDrag({x:0,y:0},{x:8,y:0}),false);assert.equal(isDrag({x:0,y:0},{x:9,y:0}),true);
 assert.equal(motionEnabled(),true);for(const options of [{reduced:true},{hardwareConcurrency:2},{deviceMemory:2}])assert.equal(motionEnabled(options),false);
});
test('published reset contracts remain valid, paused strategy and alert wake survive validation',()=>{
 const read=name=>JSON.parse(readFileSync(new URL('../paper/'+name+'.json',import.meta.url),'utf8'));
 assert.equal(validAccount(read('account')),true);assert.equal(validSnapshot(read('snapshot')),true);
 assert.equal(strategyState(read('account').accounts[0],{}),'暂停');
 const e={ts,type:'异动',coin:null,strategy:null,direction:null,price:null,reason:'放量',wake:true};
 assert.equal(validEvents({schema_version:1,events:[e]}),true);assert.equal(validEvents({schema_version:1,events:[{...e,wake:'true'}]}),false);
});
test('every state and reaction has a complete face; reactions override the state face',()=>{
 for(const state of ['idle','happy','excited','alert','sad','paused','offline'])assert.ok(FACES[state]);
 for(const f of Object.values(FACES)){assert.equal(typeof f.eyes,'string');assert.equal(typeof f.mouth,'string');assert.ok(Array.isArray(f.acc));}
 assert.deepEqual(faceFor('paused'),{eyes:'closed',mouth:'flat',acc:['zzz']});
 assert.equal(faceFor('offline').eyes,'dash');assert.notEqual(faceFor('offline').eyes,faceFor('idle').eyes);
 assert.equal(faceFor('paused','dizzy').eyes,'dizzy');assert.equal(faceFor('unknown').eyes,'normal');
});
test('drag release snaps to the nearest side and clamps the bottom offset',()=>{
 const v={width:390,height:844};
 assert.deepEqual(snapPosition({x:40,y:400},v),{side:'left',bottom:372});
 assert.deepEqual(snapPosition({x:300,y:400},v),{side:'right',bottom:372});
 assert.equal(snapPosition({x:300,y:840},v).bottom,12);
 assert.equal(snapPosition({x:300,y:-50},v).bottom,844-72-72);
});
test('narration lines: fills, wake alerts, halts, reports and route tips',()=>{
 assert.deepEqual(fillLine({symbol:'ETH-USDT-SWAP',side:1,kind:'open',price:2450.5}),{text:'开仓 ETH 多 @ 2450.5',mood:'excited'});
 assert.deepEqual(fillLine({symbol:'ETH-USDT-SWAP',side:-1,kind:'close',price:1,pnl:-2.1}),{text:'平仓 ETH 空 -2.10 USDT',mood:'sad'});
 assert.equal(fillLine({symbol:'ETH-USDT-SWAP',side:1,kind:'close',price:1,pnl:null}).mood,'happy');
 assert.equal(eventLine({type:'异动',wake:true,coin:'BTC-USDT-SWAP',reason:'放量'}).mood,'alert');
 assert.equal(eventLine({type:'异动',wake:false}),null);assert.equal(eventLine({type:'trade_opened'}),null);
 assert.equal(eventLine({type:'daily_halt'}).mood,'sad');
 assert.match(reportLine({role:'risk',title:'复盘',grade:'B+'}).text,/风控交了报告：复盘 · B\+/);
 assert.match(contextTip({route:'coin',coin:'BTC-USDT-SWAP',decision:'偏多'}),/BTC.*偏多/);
 assert.match(contextTip({route:'coin',coin:'BTC-USDT-SWAP'}),/暂无会议结论/);
 assert.match(contextTip({route:'home',paused:true}),/暂停/);assert.match(contextTip({route:'home',positions:2}),/2 个持仓/);
 assert.match(contextTip({route:'paper'}),/回撤/);assert.match(contextTip({route:'market'}),/币种/);
 assert.match(contextTip({route:'sim'}),/模拟账户/);
});
test('speech queue is bounded and drops repeats inside the dedupe window',()=>{
 let q=[];for(let i=0;i<8;i++)q=pushMessage(q,{text:'m'+i},{now:i});
 assert.deepEqual(q.map(m=>m.text),['m3','m4','m5','m6','m7']);
 q=pushMessage(q,{text:'m7'},{now:5000});assert.equal(q.length,5);
 q=pushMessage(q,{text:'m7'},{now:20000});assert.equal(q.at(-1).at,20000);
});

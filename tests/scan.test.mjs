import test from 'node:test';
import assert from 'node:assert/strict';
import {validScan,scanRows,signalText,meetingSymbol,chartUniverse,strategyEntries} from '../assets/scan.mjs';
const row=(coin,quote,side,held)=>({instId:coin+'-USDT-SWAP',quote_24h:quote,spread:.0002,leverage_cap:5,signals:{mean_reversion:side,breakout:null},signal_status:'ok',held});
const scan={schema_version:1,generated_at:'2026-11-02T00:00:00Z',instruments:[row('BTC',1e8,null,false),row('ETH',5e7,1,false),row('SOL',1e7,-1,true)],recent_signals:[],positions:[]};
test('扫描显示全部币，可排序、筛信号、筛持仓；缺失值最后',()=>{
  assert.equal(validScan(scan),true);
  assert.equal(scanRows(scan).length,3);
  assert.deepEqual(scanRows(scan,'signal').map(r=>r.instId),['ETH-USDT-SWAP','SOL-USDT-SWAP']);
  assert.equal(scanRows(scan,'held')[0].instId,'SOL-USDT-SWAP');
  assert.equal(scanRows(scan,'all','quote_24h',1)[0].instId,'SOL-USDT-SWAP');
  assert.equal(scanRows({...scan,instruments:[row('X',null,null,false),...scan.instruments]})[3].instId,'X-USDT-SWAP');
  assert.equal(signalText(scan.instruments[0]),'无');assert.match(signalText(scan.instruments[1]),/均值回归 多/);
  assert.equal(signalText({...scan.instruments[0],signal_status:'unavailable'}),'数据不足');
});
test('最新开仓优先；支持任意持仓币和信号币；空仓回退今日会议标的',()=>{
  const meeting='ZEC-USDT-SWAP';
  const account={accounts:[{id:'paper_main',status:'运行中',positions:[{symbol:'SOL-USDT-SWAP',entry_time:'2026-11-02T00:00:00Z'},{symbol:'BTC-USDT-SWAP',entry_time:'2026-11-02T01:00:00Z'}]}]};
  const result=chartUniverse(account,scan,meeting);
  assert.equal(result.defaultSymbol,'BTC-USDT-SWAP');
  assert.equal(result.choices.find(c=>c.id===meeting).meeting,true);
  assert.equal(result.choices.find(c=>c.id==='ETH-USDT-SWAP').signal,true);
  assert.equal(result.choices.find(c=>c.id==='SOL-USDT-SWAP').held,true);
  account.accounts[0].positions=[];
  assert.equal(chartUniverse(account,scan,meeting).defaultSymbol,meeting);
  assert.equal(meetingSymbol('2026-11-02-ZEC',meeting),meeting);
  assert.equal(meetingSymbol('BTC-USDT-SWAP 会议',meeting),'BTC-USDT-SWAP');
  assert.equal(meetingSymbol(null,meeting),meeting);
});
test('策略与入场方式读取快照，单策略 maker 与缺失配置可识别',()=>{
  assert.deepEqual(strategyEntries({enabled_strategies:['mean_reversion'],strategies:{mean_reversion:{},breakout:{}},entry_mode_by_strategy:{mean_reversion:'maker',breakout:'taker'}}),[{name:'mean_reversion',mode:'maker'}]);
  assert.deepEqual(strategyEntries({strategies:{breakout:{}}}),[]);
});
test('扫描契约拒绝非法方向、持仓状态和非数值成交额',()=>{
  for(const update of [{held:1},{signals:{mean_reversion:2}},{quote_24h:'100'}])assert.equal(validScan({...scan,instruments:[{...scan.instruments[0],...update}]}),false);
});

test('账户为持仓真源：实盘和空仓不混入扫描快照的旧模拟持仓',()=>{
 const stale={...scan,positions:[{instId:'OLD-USDT-SWAP',entry_time:'2026-11-02T12:00:00Z'}]};
 const live={mode:'live',accounts:[{id:'live_main',status:'运行中',positions:[]}]};
 assert.equal(chartUniverse(live,stale,'ZEC-USDT-SWAP').defaultSymbol,'ZEC-USDT-SWAP');
 assert.equal(chartUniverse(live,stale,'ZEC-USDT-SWAP').choices.some(c=>c.id==='OLD-USDT-SWAP' && c.held),false);
});
test('会议名中的裸词只在已知合约中才作为焦点币',()=>{
  const fallback='ZEC-USDT-SWAP',known=new Set(['BTC-USDT-SWAP','ETH-USDT-SWAP']);
  for(const name of ['P3 风控复盘','RSI 背离讨论','FOMC 事件会议'])assert.equal(meetingSymbol(name,fallback,known),fallback);
  assert.equal(meetingSymbol('BTC 复盘',fallback),fallback);
  assert.equal(meetingSymbol('P2 · BTC 复盘',fallback,known),'BTC-USDT-SWAP');
});

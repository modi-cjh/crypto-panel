import test from 'node:test';
import assert from 'node:assert/strict';
import {decisionsMarkup} from '../assets/decisions.mjs';
test('companion distinguishes archived wake groups, actual signals and exchange execution feedback',()=>{
 const html=decisionsMarkup({summary:{strategy_updated_at:'2026-11-03T22:00:00Z',strategy_decisions:[{ts:'2026-11-03T21:00:00Z',type:'signal',inst:'BTC-USDT-SWAP',strategy:'mean_reversion',side:1}],recent_actions:[{ts:'2026-11-03T22:00:00Z',action:'close',inst:'BTC-USDT-SWAP',reason:'execution_test',execution_test:true,price:70000,contracts:'.01',net_pnl:-.0078}]},history:{generated_at:'2026-11-02T21:00:00Z',alert_groups:[{wake_index:8,timestamp:'2026-11-02T20:00:00Z',adopted:false,alerts:[{symbol:'<script>-USDT-SWAP',type:'market',adopted:false}],orders:[]}]},status:'ready'});
 assert.match(html,/出现策略信号/);assert.match(html,/模拟成交测试/);assert.match(html,/异动 #8/);assert.match(html,/这些不是当前实时异动/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>/);
});
test('empty or unavailable evidence does not invent meetings or signals',()=>{
 assert.match(decisionsMarkup({summary:{strategy_decisions:[],recent_actions:[]},status:'error'}),/读取失败/);
 assert.match(decisionsMarkup(),/正在读取原始异动记录/);
 assert.match(decisionsMarkup(),/没有策略信号或跳过记录/);
});

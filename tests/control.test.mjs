import test from 'node:test';
import assert from 'node:assert/strict';
import { stopControlText, STOP_WORKFLOW, executionProfileText } from '../assets/control.mjs';

test('stop state is based on confirmed backend state and distinguishes partial failures', () => {
  assert.match(stopControlText(null), /需登录 GitHub/);
  assert.match(stopControlText({paused:false,stop_control:{status:'completed'}}), /需登录 GitHub/);
  assert.match(stopControlText({paused:true,stop_control:{status:'completed'}}), /已全部平仓/);
  assert.match(stopControlText({paused:true,stop_control:{status:'failed'}}), /未全部确认/);
  assert.match(stopControlText({paused:true,stop_control:{status:'pending'}}), /等待平仓回执/);
  assert.match(stopControlText({paused:true}), /仍可执行全部平仓/);
  assert.equal(new URL(STOP_WORKFLOW).hostname, 'github.com');
  assert.ok(STOP_WORKFLOW.endsWith('/actions/workflows/stop-demo.yml'));
});

test('aggressive settings and expiry are visible and expiry waits for a backend receipt', () => {
  const execution_profile={mode:'aggressive',active:true,risk:.03,same_side_cap:.06,instruments:Array(100).fill('BTC-USDT-SWAP'),scan_limit:100,until:'2026-11-04T22:20:00Z'};
  const now=Date.parse('2026-11-04T01:00:00Z');
  const text=executionProfileText({execution_profile},now);
  assert.match(text,/激进模拟档/);assert.match(text,/每笔止损风险 3%/);assert.match(text,/同向上限 6%/);assert.match(text,/100 币/);assert.match(text,/11\/05 05:20/);
  assert.match(executionProfileText({execution_profile:{...execution_profile,instruments:['BTC-USDT-SWAP']}},now),/1\/100 币（等待全量扫描）/);
  assert.match(executionProfileText({execution_profile,paused:true},now),/^已暂停/);
  assert.match(executionProfileText({execution_profile},Date.parse(execution_profile.until)),/等待任务回执确认恢复/);
  assert.match(executionProfileText(null),/等待任务回执确认/);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { stopControlText, STOP_WORKFLOW } from '../assets/control.mjs';

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

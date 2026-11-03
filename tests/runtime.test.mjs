import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeRuntimeConfig,runtimeHealthText} from '../assets/runtime.mjs';
test('runtime endpoints validate protocol, credentials and required fields',()=>{
 const raw={mode:'persistent',control_url:'https://panel.test/control',account_url:'https://panel.test/api/public/account',health_url:'https://panel.test/api/public/health'};
 assert.equal(normalizeRuntimeConfig(raw,'https://panel.test/').mode,'persistent');
 assert.equal(normalizeRuntimeConfig({...raw,control_url:'javascript:alert(1)'},'https://panel.test/').mode,'unavailable');
 assert.equal(normalizeRuntimeConfig({...raw,control_url:null},'https://panel.test/').mode,'unavailable');
 assert.equal(normalizeRuntimeConfig({...raw,control_url:'https://user:secret@panel.test/control'},'https://panel.test/').mode,'unavailable');
 assert.equal(normalizeRuntimeConfig({mode:'github_actions'},'https://panel.test/').mode,'github_actions');
});
test('heartbeat display distinguishes disabled, unknown and stale workers',()=>{
 assert.match(runtimeHealthText({mode:'github_actions'}),/可能延迟/);
 assert.match(runtimeHealthText({mode:'persistent'},null),/无法确认/);
 assert.match(runtimeHealthText({mode:'persistent'},{enabled:false}),/未启用/);
 assert.match(runtimeHealthText({mode:'persistent'},{enabled:true,worker_alive:false,heartbeat_age_seconds:0}),/异常/);
 assert.match(runtimeHealthText({mode:'persistent'},{enabled:true,worker_alive:true,heartbeat_age_seconds:20}),/异常/);
 assert.match(runtimeHealthText({mode:'persistent'},{enabled:true,worker_alive:true,heartbeat_age_seconds:1,paused:true}),/新开仓已暂停/);
});

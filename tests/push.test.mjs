// Server candle fallback (/candles) and the server push (/stream, SSE): fallback order, degrade to polling, reconnect.
import test from 'node:test';
import assert from 'node:assert/strict';
import { PublicAPI, OKX_REST_BASES, getCandles } from '../assets/api.mjs';
import { ServerPush } from '../assets/realtime.mjs';
import { LIVE_API_BASE, LIVE_STREAM_URL, streamUpdate, connectionBadge, sourceText } from '../assets/live.mjs';
import { parseCandles } from '../assets/indicators.mjs';

const ROWS = [['1794751200000', '91000.5', '91200', '90900', '91100', '12.5', '0.125', '11387.5', '0']];
const ok = data => ({ ok: true, status: 200, json: async () => ({ code: '0', msg: '', data }) });
const blocked = async () => { throw new TypeError('Failed to fetch'); };

test('candles: www.okx.com -> aws.okx.com -> server /candles', async () => {
  const calls = [];
  const api = new PublicAPI({ fetchImpl: async url => { calls.push(url); throw new TypeError('Failed to fetch'); } });
  const server = [];
  const got = await getCandles(api, 'market/candles', { instId: 'BTC-USDT-SWAP', bar: '1H', limit: 300 }, 8,
    { serverBase: LIVE_API_BASE, fetchImpl: async (url, opts) => { server.push([url, opts]); return ok(ROWS); } });
  assert.deepEqual(calls.map(u => u.split('/api/v5/')[0] + '/api/v5/'), OKX_REST_BASES);
  assert.equal(server.length, 1);
  assert.equal(server[0][0], 'https://139-180-146-42.sslip.io/candles?instId=BTC-USDT-SWAP&bar=1H&limit=300');
  assert.equal(server[0][1].credentials, 'omit');
  assert.equal(got.source, 'server');
  assert.equal(parseCandles(got.data).length, 1, 'same rows as OKX market/candles');
  // OKX now fails fast (offline for a minute): straight to the server, no new OKX request.
  const again = await getCandles(api, 'market/candles', { instId: 'BTC-USDT-SWAP', bar: '4H', limit: 300 }, 8, { serverBase: LIVE_API_BASE, fetchImpl: async () => ok(ROWS) });
  assert.equal(again.source, 'server');
  assert.equal(calls.length, 2);
});

test('candles: OKX reachable -> server never asked; history-candles keeps `after`', async () => {
  const api = new PublicAPI({ fetchImpl: async () => ok(ROWS) });
  let asked = 0;
  const got = await getCandles(api, 'market/candles', { instId: 'BTC-USDT-SWAP', bar: '1H', limit: 300 }, 8, { serverBase: LIVE_API_BASE, fetchImpl: async () => { asked++; return ok([]); } });
  assert.equal(got.source, 'okx');
  assert.equal(asked, 0);
  const down = new PublicAPI({ fetchImpl: blocked }), urls = [];
  await getCandles(down, 'market/history-candles', { instId: 'SOL-USDT-SWAP', bar: '15m', limit: '100', after: '1794751200000' }, 9, { serverBase: LIVE_API_BASE, fetchImpl: async u => { urls.push(u); return ok(ROWS); } });
  assert.equal(urls[0], LIVE_API_BASE + 'candles?instId=SOL-USDT-SWAP&bar=15m&limit=100&after=1794751200000');
  await assert.rejects(getCandles(down, 'market/tickers', {}, 1, { serverBase: LIVE_API_BASE }), /仅允许 K 线/);
});

test('candles: error only when the server fails too (original OKX reason kept)', async () => {
  const api = new PublicAPI({ fetchImpl: blocked });
  await assert.rejects(getCandles(api, 'market/candles', { instId: 'BTC-USDT-SWAP', bar: '1H' }, 8, { serverBase: LIVE_API_BASE, fetchImpl: blocked }), /网络连接失败或请求超时/);
  await assert.rejects(getCandles(api, 'market/candles', { instId: 'BTC-USDT-SWAP', bar: '1H' }, 8, { serverBase: LIVE_API_BASE, fetchImpl: async () => ({ ok: false, status: 502, json: async () => ({}) }) }));
  await assert.rejects(getCandles(api, 'market/candles', { instId: 'BTC-USDT-SWAP', bar: '1H' }, 8, { serverBase: LIVE_API_BASE, fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({ error: 'x' }) }) }));
});

// Fake EventSource + manual timers.
function harness() {
  const sources = [], timers = new Map(); let id = 0, t = 1e12;
  class FakeES { constructor(url) { this.url = url; this.closed = false; sources.push(this); } close() { this.closed = true; } }
  const states = [], messages = [];
  const push = new ServerPush({ url: LIVE_STREAM_URL, EventSourceImpl: FakeES, staleMs: 25000, now: () => t,
    setTimer: (fn, ms) => { timers.set(++id, { fn, at: t + ms }); return id; }, clearTimer: k => timers.delete(k),
    onState: s => states.push(s), onMessage: b => { messages.push(b); return Array.isArray(b.marks) ? false : true; } });
  const advance = ms => { t += ms; for (const [k, v] of [...timers].sort((a, b) => a[1].at - b[1].at)) if (v.at <= t && timers.has(k)) { timers.delete(k); v.fn(); } };
  return { push, sources, states, messages, advance, timers, emit: (body, i = -1) => sources.at(i).onmessage({ data: typeof body === 'string' ? body : JSON.stringify(body) }) };
}

test('push: live on a valid message, degraded on error, reconnects with backoff', () => {
  const h = harness();
  h.push.start();
  assert.equal(h.sources.length, 1);
  assert.equal(h.sources[0].url, 'https://139-180-146-42.sslip.io/stream');
  assert.equal(h.push.state, '连接中');
  h.emit('not json'); h.emit({ marks: [] });
  assert.equal(h.push.live, false, 'malformed / rejected messages do not make it live');
  h.emit({ ts: 1, marks: { 'SOL-USDT-SWAP': 70.5 } });
  assert.equal(h.push.live, true);
  h.sources[0].onerror();
  assert.equal(h.push.state, '断开');
  assert.equal(h.push.live, false);
  assert.ok(h.sources[0].closed, 'errored source closed (page falls back to 60 s polling)');
  h.advance(2999); assert.equal(h.sources.length, 1);
  h.advance(1); assert.equal(h.sources.length, 2, 'reconnect after 3 s');
  h.sources[1].onerror(); h.advance(5999); assert.equal(h.sources.length, 2);
  h.advance(1); assert.equal(h.sources.length, 3, 'then 6 s');
  h.emit({ ts: 2, marks: {} });
  assert.equal(h.push.live, true);
  assert.equal(h.push.failures, 0, 'backoff reset after data');
});

test('push: silent connection times out and reconnects; stop closes everything', () => {
  const h = harness();
  h.push.start();
  h.emit({ ts: 1, marks: {} });
  h.advance(24000); h.emit({ ts: 2, marks: {} });
  h.advance(24000); assert.equal(h.push.live, true, 'messages keep it alive');
  h.advance(1001);
  assert.equal(h.push.state, '断开');
  assert.ok(h.sources[0].closed);
  h.advance(3000); assert.equal(h.sources.length, 2);
  h.push.stop();
  assert.ok(h.sources[1].closed);
  assert.equal(h.timers.size, 0);
  h.advance(120000); assert.equal(h.sources.length, 2, 'no reconnect after stop');
  // stale events from a closed source are ignored
  h.sources[0].onmessage?.({ data: '{}' });
});

test('push: no EventSource support -> stays on polling', () => {
  const p = new ServerPush({ url: LIVE_STREAM_URL, EventSourceImpl: undefined });
  p.start();
  assert.equal(p.state, '断开');
  assert.equal(p.live, false);
});

test('stream messages: only the positions, only fresh, valid prices', () => {
  const now = 1794751218000;
  const u = streamUpdate({ ts: now - 1000, marks: { 'SOL-USDT-SWAP': 70.5, 'BTC-USDT-SWAP': 91000, '<x>': 1, 'ETH-USDT-SWAP': -1 }, live_at: '2026-11-15T14:02:46Z' }, ['SOL-USDT-SWAP', 'ETH-USDT-SWAP'], now);
  assert.deepEqual(u.marks, [['SOL-USDT-SWAP', 70.5]]);
  assert.equal(u.liveAt, '2026-11-15T14:02:46Z');
  assert.deepEqual(streamUpdate({ ts: now - 31000, marks: { 'SOL-USDT-SWAP': 70.5 } }, ['SOL-USDT-SWAP'], now).marks, [], 'old prices dropped');
  assert.deepEqual(streamUpdate({ ts: null, marks: {}, live_at: 'x' }, [], now), { marks: [], liveAt: null });
  assert.equal(streamUpdate({ ts: now }, [], now), null);
  assert.equal(streamUpdate({ ts: now, marks: [] }, [], now), null);
  assert.equal(streamUpdate(null, [], now), null);
});

test('badge and source text name the push', () => {
  assert.deepEqual(connectionBadge({ push: true, ws: true }), { text: '实时推送', ok: true });
  assert.deepEqual(connectionBadge({ ws: true }), { text: '行情实时', ok: true });
  assert.match(sourceText('api', 'okx', true), /实时推送.*每分钟/);
  assert.doesNotMatch(sourceText('api', 'okx'), /推送/);
});

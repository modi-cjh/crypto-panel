// Fallbacks for networks that block OKX (Cambodian 4G): REST base rotation, server API / tickers parsing, connection badge.
import test from 'node:test';
import assert from 'node:assert/strict';
import { PublicAPI, OKX_REST_BASES, normalizeTickers } from '../assets/api.mjs';
import { splitLiveApi, serverTickerRows, connectionBadge, sourceText, LIVE_API_URL, LIVE_TICKERS_URL } from '../assets/live.mjs';

const ok = data => ({ ok: true, status: 200, json: async () => ({ code: '0', data }) });
const iso = ms => new Date(ms).toISOString().replace(/\.\d+Z$/, 'Z');

test('REST: www.okx.com blocked -> aws.okx.com, which is then tried first', async () => {
  const calls = [];
  const api = new PublicAPI({ fetchImpl: async url => { calls.push(url); if (url.startsWith(OKX_REST_BASES[0])) throw new TypeError('Failed to fetch'); return ok([{ instId: 'BTC-USDT-SWAP', last: '1' }]); } });
  assert.deepEqual(await api.get('market/tickers', { instType: 'SWAP' }), [{ instId: 'BTC-USDT-SWAP', last: '1' }]);
  assert.equal(calls.length, 2);
  assert.ok(calls[1].startsWith('https://aws.okx.com/api/v5/market/tickers?instType=SWAP'));
  assert.equal(api.base, OKX_REST_BASES[1]);
  await api.get('market/candles', { instId: 'BTC-USDT-SWAP' });
  assert.ok(calls[2].startsWith('https://aws.okx.com/'), 'working base first');
});

test('REST: every base down -> fails fast for a minute (no repeated timeouts)', async () => {
  let t = 1e6, calls = 0;
  const api = new PublicAPI({ now: () => t, fetchImpl: async () => { calls++; throw new TypeError('Failed to fetch'); } });
  await assert.rejects(api.get('market/tickers', { instType: 'SWAP' }), /网络连接失败或请求超时/);
  assert.equal(calls, 2);
  assert.equal(api.base, null);
  await assert.rejects(api.get('market/tickers', { instType: 'SWAP' }), /OKX 公共接口暂时连不上/);
  assert.equal(calls, 2, 'no network call while offline');
  t += 61000;
  await assert.rejects(api.get('market/tickers', { instType: 'SWAP' }));
  assert.equal(calls, 4, 'retried after a minute');
});

test('REST: an OKX error answer does not switch domains', async () => {
  const calls = [];
  const api = new PublicAPI({ fetchImpl: async url => { calls.push(url); return { ok: false, status: 429, json: async () => ({}) }; } });
  await assert.rejects(api.get('market/tickers', { instType: 'SWAP' }), /请求受限/);
  assert.equal(calls.length, 1);
  await assert.rejects(api.get('account/balance'), /只读公共行情/);
});

test('server /live.json split into the live-data documents', () => {
  const snapshot = { trading_mode: 'live', updated_at: iso(Date.now()), equity: 130.5, positions: [{ inst: 'SOL-USDT-SWAP', side: 'short' }] };
  const doc = { source: 'okx-live-api', api: { account: 'okx' }, snapshot, fills: { fills: [], events: [] }, trades: { trades: [], stats: {} } };
  const s = splitLiveApi(doc);
  assert.equal(s.snapshot.equity, 130.5);
  assert.deepEqual(s.fills.fills, []);
  assert.equal(s.account, 'okx');
  assert.equal(splitLiveApi({ ...doc, fills: { bad: 1 }, api: { account: 'file' } }).fills, null);
  assert.equal(splitLiveApi({ ...doc, api: { account: 'file' } }).account, 'file');
  assert.equal(splitLiveApi({ ...doc, source: 'okx-live' }), null);
  assert.equal(splitLiveApi({ ...doc, snapshot: { ...snapshot, trading_mode: 'demo' } }), null);
  assert.equal(splitLiveApi(null), null);
  assert.match(LIVE_API_URL, /^https:\/\/139-180-146-42\.sslip\.io\/live\.json$/);
  assert.match(LIVE_TICKERS_URL, /^https:\/\/139-180-146-42\.sslip\.io\/tickers\.json$/);
});

test('server tickers: valid rows only, stale document refused, same shape as OKX tickers', () => {
  const now = Date.now();
  const doc = { updated_at: iso(now - 60e3), tickers: [{ instId: 'BTC-USDT-SWAP', last: 91000, open24h: 90000, volCcy24h: 10 }, { instId: '<x>', last: 1 }, { instId: 'ETH-USDT-SWAP', last: 0 }] };
  const rows = serverTickerRows(doc, now);
  assert.equal(rows.length, 1);
  const [t] = normalizeTickers(rows);
  assert.equal(t.id, 'BTC-USDT-SWAP');
  assert.equal(t.turnover, 910000);
  assert.ok(Math.abs(t.change - 1.1111) < 1e-3);
  assert.equal(serverTickerRows({ ...doc, updated_at: iso(now - 10 * 60e3) }, now), null);
  assert.equal(serverTickerRows({ updated_at: iso(now), tickers: [] }, now), null);
  assert.equal(serverTickerRows(null, now), null);
});

test('connection badge: red only when every source failed', () => {
  assert.deepEqual(connectionBadge({ ws: true }), { text: '行情实时', ok: true });
  assert.deepEqual(connectionBadge({ rest: true }), { text: '行情轮询', ok: true });
  assert.deepEqual(connectionBadge({ server: true }), { text: '服务器行情', ok: true });
  assert.deepEqual(connectionBadge({}), { text: '行情断开', ok: false });
  assert.match(sourceText('api', 'okx'), /服务器只读接口.*每分钟/);
  assert.match(sourceText('github'), /live-data/);
});

test('REST: fail-fast is per endpoint (blocked tickers do not stop a candle request)', async () => {
  let block = true, calls = 0;
  const api = new PublicAPI({ fetchImpl: async () => { calls++; if (block) throw new TypeError('Failed to fetch'); return ok([]); } });
  await assert.rejects(api.get('market/tickers', { instType: 'SWAP' }));
  block = false;
  assert.deepEqual(await api.get('market/candles', { instId: 'BTC-USDT-SWAP' }), []);
  await assert.rejects(api.get('market/tickers', { instType: 'SWAP' }), /暂时连不上/);
});

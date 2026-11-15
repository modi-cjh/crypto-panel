// OKX public REST: the main domain first, then OKX's AWS domain (some mobile networks block one but not the other).
// The base that last worked is tried first; when every base fails for an endpoint, that endpoint fails fast for OFFLINE_MS
// so the page can switch to the server fallback (tickers.json) without waiting on timeouts again.
export const OKX_REST_BASES = ['https://www.okx.com/api/v5/', 'https://aws.okx.com/api/v5/'];
const OFFLINE_MS = 60000, TIMEOUT_MS = 8000;
const ALLOWED = new Set(['market/tickers', 'market/candles', 'market/history-candles', 'public/funding-rate', 'public/open-interest']);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// 所有公共请求共享队列：启动间隔 >= 150ms，最多 3 个并发，无自动重试风暴。
export class PublicAPI {
  constructor({ bases = OKX_REST_BASES, fetchImpl = (...a) => fetch(...a), now = () => Date.now() } = {}) {
    this.queue = []; this.active = 0; this.lastStart = 0; this.cooldown = 0; this.pumping = false; this.pending = new Map();
    this.bases = bases; this.preferred = 0; this.offline = new Map(); this.fetch = fetchImpl; this.now = now;   // offline: path -> fail-fast until
  }
  // Base URL that served the last successful request (for the diagnostics), or null after a full failure.
  get base() { return this.offline.get('market/tickers') > this.now() ? null : this.bases[this.preferred]; }
  get(path, params = {}, priority = 0) {
    if (!ALLOWED.has(path)) return Promise.reject(new Error('仅允许只读公共行情接口'));
    if (this.offline.get(path) > this.now()) return Promise.reject(new Error('OKX 公共接口暂时连不上'));
    const url = path + '?' + new URLSearchParams(params);
    if (this.pending.has(url)) return this.pending.get(url);
    const promise = new Promise((resolve, reject) => {
      this.queue.push({ path, url, priority, resolve, reject });
      this.queue.sort((a, b) => b.priority - a.priority);
      this.pump();
    }).finally(() => this.pending.delete(url));
    this.pending.set(url, promise);
    return promise;
  }
  async pump() {
    if (this.pumping) return;
    this.pumping = true;
    while (this.queue.length && this.active < 3) {
      await sleep(Math.max(0, this.lastStart + 150 - Date.now(), this.cooldown - Date.now()));
      // 等待期间新加入的高优先级请求也能优先执行。
      const job = this.queue.shift(); this.active++; this.lastStart = Date.now();
      this.run(job).finally(() => { this.active--; this.pump(); });
    }
    this.pumping = false;
  }
  async run(job) {
    // Network failures (blocked, timeout, DNS) move on to the next base; an answer from OKX (rate limit, error code) does not.
    const order = this.bases.map((_, i) => (this.preferred + i) % this.bases.length);
    let lastError = null;
    for (const i of order) {
      let response;
      try { response = await this.fetch(this.bases[i] + job.url, { method: 'GET', credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(TIMEOUT_MS) }); }
      catch (error) { lastError = error; continue; }
      try {
        if (response.status === 429) this.cooldown = Date.now() + 5000;
        if (!response.ok) throw new Error(response.status === 429 ? '请求受限，稍后自动刷新' : `接口响应异常（${response.status}）`);
        const body = await response.json();
        if (body.code === '50011') this.cooldown = Date.now() + 5000;
        if (body.code !== '0' || !Array.isArray(body.data)) throw new Error(`OKX 接口异常（${body.code || '格式错误'}）`);
        this.preferred = i; this.offline.delete(job.path);
        return job.resolve(body.data);
      } catch (error) { return job.reject(error); }
    }
    this.offline.set(job.path, this.now() + OFFLINE_MS);
    job.reject(lastError && /^(OKX|接口|请求)/.test(lastError.message) ? lastError : new Error('网络连接失败或请求超时'));
  }
}

export function number(value) { return value === '' || value == null || !Number.isFinite(Number(value)) ? null : Number(value); }
export function normalizeTickers(rows) {
  return rows.filter(r => /^[A-Z0-9]+-USDT-SWAP$/.test(r.instId) && number(r.last) > 0).map(r => {
    const last = number(r.last), open = number(r.open24h), volume = number(r.volCcy24h);
    return { id: r.instId, symbol: r.instId.split('-')[0], last, change: open > 0 ? (last / open - 1) * 100 : null, turnover: volume === null ? null : volume * last, timestamp: number(r.ts) };
  });
}
export function positionPnL(position, price, capped = false) {
  if (!Number.isFinite(price)) return null;
  const pnl = position.side * position.qty * (price - position.entry);
  return capped && Number.isFinite(position.margin) ? Math.max(-position.margin, pnl) : pnl;
}

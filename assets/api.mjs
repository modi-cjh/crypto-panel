const BASE = 'https://www.okx.com/api/v5/';
const ALLOWED = new Set(['market/tickers', 'market/candles', 'market/history-candles', 'public/funding-rate', 'public/open-interest']);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// 所有公共请求共享队列：启动间隔 >= 150ms，最多 3 个并发，无自动重试风暴。
export class PublicAPI {
  constructor() { this.queue = []; this.active = 0; this.lastStart = 0; this.cooldown = 0; this.pumping = false; this.pending = new Map(); }
  get(path, params = {}, priority = 0) {
    if (!ALLOWED.has(path)) return Promise.reject(new Error('仅允许只读公共行情接口'));
    const url = BASE + path + '?' + new URLSearchParams(params);
    if (this.pending.has(url)) return this.pending.get(url);
    const promise = new Promise((resolve, reject) => {
      this.queue.push({ url, priority, resolve, reject });
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
    try {
      const response = await fetch(job.url, { method: 'GET', credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(12000) });
      if (response.status === 429) this.cooldown = Date.now() + 5000;
      if (!response.ok) throw new Error(response.status === 429 ? '请求受限，稍后自动刷新' : `接口响应异常（${response.status}）`);
      const body = await response.json();
      if (body.code === '50011') this.cooldown = Date.now() + 5000;
      if (body.code !== '0' || !Array.isArray(body.data)) throw new Error(`OKX 接口异常（${body.code || '格式错误'}）`);
      job.resolve(body.data);
    } catch (error) { job.reject(error); }
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

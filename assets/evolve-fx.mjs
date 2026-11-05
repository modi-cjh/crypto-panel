// Gene-pool ambient motion drawn on a canvas laid over the static SVG.
// Animating hundreds of SVG elements with CSS (scale, dash offsets) makes mobile Safari repaint the whole graph every
// frame, which stutters. Here the SVG never changes while idle; one requestAnimationFrame loop draws soft halos around
// living strategies and an occasional glowing spark travelling parent → child along each lineage line into them.

const TAU = Math.PI * 2;
export const easeInOut = u => .5 - .5 * Math.cos(Math.PI * Math.min(1, Math.max(0, u)));

// Halo strength in [0, 1] for a node breathing with its own period (s) and phase (0–1).
export function breath(t, period, phase) { return .5 + .5 * Math.sin(TAU * (t / period + phase)); }

// Spark progress along an edge: travels during the first `active` share of each period (eased), then rests.
// Returns null while resting, else { u: position 0–1 along the edge, a: opacity 0–1 }.
export function sparkAt(t, period, phase, active = .38) {
  const c = ((t / period + phase) % 1 + 1) % 1;
  if (c >= active) return null;
  const k = c / active;
  return { u: easeInOut(k), a: Math.sin(Math.PI * k) };
}

// Stable pseudo-random 0–1 from a string, so every node / edge keeps its own rhythm across redraws.
export function seed(s) { let h = 2166136261; for (const ch of String(s)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return ((h >>> 0) % 10007) / 10007; }

function glowSprite(color, size = 64) {
  const c = document.createElement('canvas'); c.width = c.height = size;
  const g = c.getContext('2d'), r = size / 2, grad = g.createRadialGradient(r, r, 0, r, r, r);
  grad.addColorStop(0, '#ffffff'); grad.addColorStop(.18, color); grad.addColorStop(.5, color + '55'); grad.addColorStop(1, color + '00');
  g.fillStyle = grad; g.fillRect(0, 0, size, size); return c;
}

export class GeneFx {
  constructor(box, { still = () => false, active = () => true } = {}) {
    this.box = box; this.still = still; this.active = active; this.frame = 0; this.visible = true; this.svg = null; this.items = null;
    this.spark = null;
    if ('IntersectionObserver' in window) new IntersectionObserver(es => { this.visible = es.some(e => e.isIntersecting); this.kick(); }).observe(box);
    document.addEventListener('visibilitychange', () => this.kick());
    addEventListener('resize', () => { this.items = null; this.kick(); }, { passive: true });
  }
  // Called after the graph markup is (re)written.
  refresh() { this.items = null; this.kick(); }
  kick() { if (!this.frame && this.running()) this.frame = requestAnimationFrame(t => this.draw(t)); }
  running() { return this.visible && !document.hidden && !this.still() && this.active(); }
  collect() {
    const svg = this.box.querySelector('svg.evo-graph'), canvas = this.box.querySelector('canvas.evo-fx');
    if (!svg || !canvas) return null;
    const rect = svg.getBoundingClientRect(), dpr = Math.min(1.5, devicePixelRatio || 1);
    if (!rect.width) return null;
    const vb = svg.viewBox.baseVal, s = Math.min(rect.width / vb.width, rect.height / vb.height);
    const ox = (rect.width - vb.width * s) / 2, oy = (rect.height - vb.height * s) / 2, X = x => ox + x * s, Y = y => oy + y * s;
    canvas.width = Math.round(rect.width * dpr); canvas.height = Math.round(rect.height * dpr);
    const nodes = [...svg.querySelectorAll('.evo-node.alive')].map(g => {
      const c = g.querySelector('circle:not(.hit)'), id = g.dataset.node;
      return { g, x: X(+c.getAttribute('cx')), y: Y(+c.getAttribute('cy')), r: +c.getAttribute('r') * s, color: c.getAttribute('fill'), period: 4.5 + seed(id + 'p') * 3.5, phase: seed(id) };
    });
    const edges = [...svg.querySelectorAll('.evo-edge.flow')].map((l, i) => {
      const k = l.getAttribute('x1') + l.getAttribute('y1') + l.getAttribute('x2') + i;
      return { l, x1: X(+l.getAttribute('x1')), y1: Y(+l.getAttribute('y1')), x2: X(+l.getAttribute('x2')), y2: Y(+l.getAttribute('y2')), period: 6 + seed(k + 'p') * 7, phase: seed(k) };
    });
    return { svg, canvas, ctx: canvas.getContext('2d'), dpr, s, nodes, edges };
  }
  draw(now) {
    this.frame = 0;
    if (!this.running()) { this.clear(); return; }
    if (!this.items || !this.items.svg.isConnected) this.items = this.collect();
    const it = this.items;
    if (it) {
      const { ctx, dpr, s } = it, t = now / 1000;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, it.canvas.width, it.canvas.height);
      if (!it.svg.classList.contains('replay')) {           // the replay has its own motion; ambient effects resume after it
        for (const n of it.nodes) {                        // a soft ring that slowly widens and fades on each node's own rhythm
          const b = breath(t, n.period, n.phase);
          ctx.globalAlpha = .38 - .24 * b; ctx.strokeStyle = n.color; ctx.lineWidth = 2 * Math.max(.7, s);
          ctx.beginPath(); ctx.arc(n.x, n.y, n.r + (2 + 5 * b) * Math.max(.6, s), 0, TAU); ctx.stroke();
        }
        this.spark ||= glowSprite('#2fb7c4');
        const size = 16 * Math.max(.7, s);
        for (const e of it.edges) {
          const p = sparkAt(t, e.period, e.phase); if (!p) continue;
          for (let k = 3; k >= 0; k--) {                   // a short fading tail behind the spark
            const u = Math.max(0, p.u - k * .035), x = e.x1 + (e.x2 - e.x1) * u, y = e.y1 + (e.y2 - e.y1) * u, sz = size * (1 - k * .18);
            ctx.globalAlpha = p.a * (1 - k * .24);
            ctx.drawImage(this.spark, x - sz / 2, y - sz / 2, sz, sz);
          }
        }
        ctx.globalAlpha = 1;
      }
    }
    this.frame = requestAnimationFrame(t2 => this.draw(t2));
  }
  clear() { const c = this.box.querySelector('canvas.evo-fx'); if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height); }
}

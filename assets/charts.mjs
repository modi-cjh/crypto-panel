import { ema, rsi, macd } from './indicators.mjs';
export const COLORS = ['#06727d', '#345db5', '#916000', '#7654b3', '#087c50', '#a44a16', '#a43276', '#526273'];
const up = '#087c50', down = '#c53045';
const formatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Bangkok', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
export function chartAvailable() { return Boolean(globalThis.LightweightCharts); }
function makeChart(element) {
  const chart = LightweightCharts.createChart(element, {
    width: element.clientWidth, height: element.clientHeight,
    layout: { background: { color: '#ffffff' }, textColor: '#4b5563', fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
    grid: { vertLines: { color: '#edf0f3' }, horzLines: { color: '#edf0f3' } },
    rightPriceScale: { borderColor: '#d1d5db', minimumWidth: 78 },
    timeScale: { borderColor: '#d1d5db', timeVisible: true, secondsVisible: false, rightOffset: 3,
      tickMarkFormatter: time => formatter.format(new Date(Number(time) * 1000)) },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { locale: 'zh-CN', timeFormatter: time => formatter.format(new Date(Number(time) * 1000)) },
    handleScroll: { vertTouchDrag: false }, handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
  });
  const observer = new ResizeObserver(() => chart.applyOptions({ width: element.clientWidth, height: element.clientHeight }));
  observer.observe(element);
  return { chart, destroy() { observer.disconnect(); chart.remove(); } };
}
function line(chart, color, title, options = {}) {
  return chart.addLineSeries({ color, title, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, ...options });
}
function seriesData(bars, values) { return bars.flatMap((b, i) => Number.isFinite(values[i]) ? [{ time: b.time, value: values[i] }] : []); }
export function createCandleCharts() {
  const groups = ['price', 'volume', 'rsi', 'macd'].map(name => makeChart(document.getElementById(`${name}-chart`)));
  const [price, volume, strength, convergence] = groups.map(g => g.chart);
  const candles = price.addCandlestickSeries({ upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down, priceLineVisible: false });
  const averages = [line(price, up, 'EMA20'), line(price, '#916000', 'EMA50'), line(price, '#7654b3', 'EMA200')];
  const vol = volume.addHistogramSeries({ priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false });
  const rs = line(strength, '#7654b3', 'RSI14');
  for (const value of [30, 70]) rs.createPriceLine({ price: value, color: '#536176', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' });
  const dif = line(convergence, '#345db5', 'DIF'), dea = line(convergence, '#916000', 'DEA');
  const hist = convergence.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false, priceFormat: { type: 'price', precision: 6, minMove: .000001 } });
  let syncing = false, initialized = false, priceLines = [];
  let overlays=[];
  candles.applyOptions({autoscaleInfoProvider: original => {
    const info=original(); if(!info)return info;
    for(const row of overlays){info.priceRange.minValue=Math.min(info.priceRange.minValue,row.price);info.priceRange.maxValue=Math.max(info.priceRange.maxValue,row.price);}
    return info;
  }});
  const subscriptions = groups.map(({ chart }) => {
    const handler = range => {
      if (!range || syncing) return;
      syncing = true;
      for (const target of groups) if (target.chart !== chart) target.chart.timeScale().setVisibleLogicalRange(range);
      syncing = false;
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(handler);
    return handler;
  });
  return {
    update(bars) {
      const range = initialized ? price.timeScale().getVisibleLogicalRange() : null;
      const closes = bars.map(b => b.close), mc = macd(closes);
      const minimum = Math.min(...closes), precision = minimum >= 100 ? 2 : minimum >= 1 ? 4 : minimum >= .01 ? 6 : 8;
      const priceFormat = { type: 'price', precision, minMove: 10 ** -precision };
      candles.applyOptions({ priceFormat }); averages.forEach(a => a.applyOptions({ priceFormat }));
      candles.setData(bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
      [20, 50, 200].forEach((period, i) => averages[i].setData(seriesData(bars, ema(closes, period))));
      vol.setData(bars.map(b => ({ time: b.time, value: b.volume, color: b.close >= b.open ? '#7ee7c380' : '#f3818c80' })));
      // 空值用 whitespace 对齐全部副图的逻辑索引。
      const aligned = values => bars.map((b, i) => Number.isFinite(values[i]) ? { time: b.time, value: values[i] } : { time: b.time });
      rs.setData(aligned(rsi(closes))); dif.setData(aligned(mc.dif)); dea.setData(aligned(mc.dea));
      hist.setData(bars.map((b, i) => mc.histogram[i] === null ? { time: b.time } : { time: b.time, value: mc.histogram[i], color: mc.histogram[i] >= 0 ? '#7ee7c380' : '#f3818c80' }));
      price.timeScale().setVisibleLogicalRange(range || { from: Math.max(0, bars.length - 100), to: bars.length + 2 });
      initialized = true;
    },
    markers(rows) { candles.setMarkers(rows); },
    levels(rows) {
      priceLines.forEach(p=>candles.removePriceLine(p));overlays=rows;
      priceLines=rows.map(row=>candles.createPriceLine({...row,lineWidth:1,lineStyle:2,axisLabelVisible:true}));
    },
    reset() { price.timeScale().fitContent(); },
    destroy() { groups.forEach((g, i) => { g.chart.timeScale().unsubscribeVisibleLogicalRangeChange(subscriptions[i]); g.destroy(); }); },
  };
}
export function createEquityChart(accounts) {
  const group = makeChart(document.getElementById('equity-chart'));
  accounts.forEach((account, i) => {
    const series = line(group.chart, account.status === '已停止' ? '#687266' : COLORS[i % COLORS.length], account.name, { lineWidth: 2, lineStyle: account.status === '已停止' ? 2 : 0, priceFormat: { type: 'price', precision: 2, minMove: .01 } });
    const unique = new Map(account.equity.map(p => [Math.floor(Date.parse(p.timestamp) / 1000), p.equity]));
    series.setData([...unique].sort(([a], [b]) => a - b).map(([time, value]) => ({ time, value })));
  });
  group.chart.timeScale().fitContent();
  return group;
}

// The home balance curve contains only the current public snapshot's accounts.
export function createBalanceChart(points) {
  const group=makeChart(document.getElementById('equity-chart'));
  const series=group.chart.addAreaSeries({topColor:'#06727d48',bottomColor:'#06727d02',lineColor:'#06727d',lineWidth:2,priceLineVisible:false,lastValueVisible:false});
  series.setData(points);
  group.chart.timeScale().fitContent();
  return group;
}

// A single candle pane for the position / closed-trade popup: entry / exit markers, stop / target / entry lines.
export function createTradeChart(element) {
  const group = makeChart(element);
  const candles = group.chart.addCandlestickSeries({ upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down, priceLineVisible: false });
  let overlays = [];
  candles.applyOptions({ autoscaleInfoProvider: original => {
    const info = original(); if (!info) return info;
    for (const row of overlays) { info.priceRange.minValue = Math.min(info.priceRange.minValue, row.price); info.priceRange.maxValue = Math.max(info.priceRange.maxValue, row.price); }
    return info;
  } });
  return {
    draw(bars, levels, markers) {
      const lows = bars.map(b => b.low).concat(levels.map(l => l.price)), minimum = Math.min(...lows);
      const precision = minimum >= 100 ? 2 : minimum >= 1 ? 4 : minimum >= .01 ? 6 : 8;
      candles.applyOptions({ priceFormat: { type: 'price', precision, minMove: 10 ** -precision } });
      candles.setData(bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
      overlays = levels;
      levels.forEach(l => candles.createPriceLine({ price: l.price, color: l.color, title: l.title, lineWidth: 1, lineStyle: l.role === 'entry' ? 0 : 2, axisLabelVisible: true }));
      candles.setMarkers(markers);
      group.chart.timeScale().fitContent();
    },
    destroy() { group.destroy(); },
  };
}

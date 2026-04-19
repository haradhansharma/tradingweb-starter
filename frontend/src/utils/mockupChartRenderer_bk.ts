/**
 * Mockup Chart Renderer — Generic Multi-Pane Chart System
 * =======================================================
 * Creates a price pane + auto-generated sub-panes for oscillators,
 * areas, and histograms — all driven by chart_meta from config.
 *
 * No indicator names are hardcoded. The renderer reads chart_meta
 * from each indicator slot and places it on the correct pane.
 *
 * Usage:
 *   initMockupTimeframeCard(container, candles, indicatorSlots, indicatorHistory, options)
 *   destroyMockupTimeframeCard(containerId)
 *
 * Real data mode:
 *   fetchKlinesForTimeframes(symbol, timeframes, broker) → { [tf]: MockCandle[] }
 *   computeEmaFromCandles(candles, period) → Array<{ time, value }>
 *   computeSmaFromCandles(candles, period) → Array<{ time, value }>
 *   buildRealIndicatorHistory(candles, slots, realIndicators) → { [key]: [{time, value}] }
 */

import {
  createChart, ColorType,
  AreaSeries, CandlestickSeries, HistogramSeries, LineSeries,
} from 'lightweight-charts';

// ── Types (inline — no external mockData dependency) ──

export interface MockCandle {
  t: number; o: number; h: number; l: number; c: number; v: number;
}

export interface IndicatorSlot {
  key: string;
  label: string;
  chart_meta: {
    pane: string;
    series: string;
    color: string;
    y_range?: number[];
    ref_lines: number[];
    line_width?: number;
    line_style?: number;
  };
  format?: string;
  decimals?: number;
  color_rules?: any[];
  default_class?: string;
  bold?: boolean;
  font_size?: string;
}

export interface MockupChartOptions {
  symbol: string;
  timeframe: string;
  darkMode?: boolean;
  srLevels?: {
    support?: number | null;
    resistance?: number | null;
    support_secondary?: number | null;
    resistance_secondary?: number | null;
  };
}

interface ChartGroup {
  /** Price pane chart instance (always exists) */
  priceChart: any;
  /** Sub-pane charts by pane type */
  subPanes: Record<string, any>;
}

// Store chart groups for cleanup
const chartGroups: Record<string, ChartGroup> = {};

// ── Data Deduplication Helper ──

/**
 * Deduplicate data by time field — keep the LAST entry for each timestamp.
 * Lightweight Charts requires strictly ascending unique time values.
 */
function deduplicateByTime<T extends { time: any }>(data: T[]): T[] {
  if (data.length === 0) return data;
  const seen = new Map<any, T>();
  for (const item of data) {
    seen.set(item.time, item); // later entries overwrite earlier ones
  }
  const result = Array.from(seen.values());
  // Sort by time ascending (should already be, but ensure it)
  result.sort((a, b) => (a.time as number) - (b.time as number));
  return result;
}

/**
 * Filter out data points where value is NaN, null, or undefined.
 * Lightweight Charts crashes on NaN values — this prevents that.
 */
function sanitizeSeriesData(data: Array<{ time: any; value: number }>): Array<{ time: any; value: number }> {
  return data.filter(p => p.value != null && !Number.isNaN(p.value) && Number.isFinite(p.value));
}

// ═══════════════════════════════════════════════════════
//  REAL DATA HELPERS — compute indicators from candle data
// ═══════════════════════════════════════════════════════

/**
 * Fetch kline candles from REST API for multiple timeframes.
 * Returns { [timeframe]: MockCandle[] }
 */
export async function fetchKlinesForTimeframes(
  symbol: string,
  timeframes: Array<{ key: string }>,
  broker: string,
  limit: number = 100,
): Promise<Record<string, MockCandle[]>> {
  const results: Record<string, MockCandle[]> = {};
  const brokerParam = broker || 'binance';

  const promises = timeframes.map(async (tf) => {
    try {
      const resp = await fetch(
        `/api/market/klines/${symbol}?interval=${tf.key}&limit=${limit}&broker=${brokerParam}`
      );
      if (!resp.ok) {
        console.warn(`[Mockup] Kline fetch failed for ${tf.key}:`, resp.status);
        return;
      }
      const data = await resp.json();
      const candles: MockCandle[] = (data.candles || []).map((c: any) => ({
        t: c.t, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v,
      }));
      if (candles.length > 0) results[tf.key] = candles;
    } catch (e) {
      console.error(`[Mockup] Kline fetch error for ${tf.key}:`, e);
    }
  });

  await Promise.all(promises);
  return results;
}

/**
 * Compute EMA (Exponential Moving Average) from candle closes.
 * Returns time-series array matching candle timestamps.
 */
export function computeEmaFromCandles(
  candles: MockCandle[],
  period: number,
): Array<{ time: number; value: number }> {
  if (candles.length < period) return [];
  const k = 2 / (period + 1);
  const points: Array<{ time: number; value: number }> = [];
  // Seed EMA with SMA of first `period` candles
  let ema = 0;
  for (let i = 0; i < period; i++) {
    ema += candles[i].c;
  }
  ema /= period;
  points.push({ time: Math.floor(candles[period - 1].t / 1000), value: +ema.toFixed(2) });

  for (let i = period; i < candles.length; i++) {
    ema = candles[i].c * k + ema * (1 - k);
    points.push({ time: Math.floor(candles[i].t / 1000), value: +ema.toFixed(2) });
  }
  return points;
}

/**
 * Compute SMA (Simple Moving Average) from candle closes.
 */
export function computeSmaFromCandles(
  candles: MockCandle[],
  period: number,
): Array<{ time: number; value: number }> {
  if (candles.length < period) return [];
  const points: Array<{ time: number; value: number }> = [];
  for (let i = period - 1; i < candles.length; i++) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += candles[j].c;
    points.push({
      time: Math.floor(candles[i].t / 1000),
      value: +(sum / period).toFixed(2),
    });
  }
  return points;
}

/**
 * Compute Bollinger Bands from candle closes.
 * Returns { upper, middle, lower } each as time-series.
 */
export function computeBollingerBands(
  candles: MockCandle[],
  period: number = 20,
  stdDev: number = 2,
): { upper: Array<{ time: number; value: number }>; middle: Array<{ time: number; value: number }>; lower: Array<{ time: number; value: number }> } {
  const middle = computeSmaFromCandles(candles, period);
  if (middle.length === 0) return { upper: [], middle: [], lower: [] };

  const startIdx = period - 1;
  const upper: Array<{ time: number; value: number }> = [];
  const lower: Array<{ time: number; value: number }> = [];

  for (let i = 0; i < middle.length; i++) {
    const candleIdx = startIdx + i;
    let sumSq = 0;
    for (let j = candleIdx - period + 1; j <= candleIdx; j++) {
      sumSq += Math.pow(candles[j].c - middle[i].value, 2);
    }
    const sd = Math.sqrt(sumSq / period) * stdDev;
    upper.push({ time: middle[i].time, value: +(middle[i].value + sd).toFixed(2) });
    lower.push({ time: middle[i].time, value: +(middle[i].value - sd).toFixed(2) });
  }

  return { upper, middle, lower };
}

/**
 * Compute RSI from candle closes.
 */
export function computeRsi(
  candles: MockCandle[],
  period: number = 14,
): Array<{ time: number; value: number }> {
  if (candles.length < period + 1) return [];
  const points: Array<{ time: number; value: number }> = [];
  let avgGain = 0, avgLoss = 0;

  // Seed with first `period` changes
  for (let i = 1; i <= period; i++) {
    const change = candles[i].c - candles[i - 1].c;
    if (change >= 0) avgGain += change; else avgLoss += Math.abs(change);
  }
  avgGain /= period;
  avgLoss /= period;

  let rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
  let rsi = 100 - 100 / (1 + rs);
  points.push({ time: Math.floor(candles[period].t / 1000), value: +rsi.toFixed(2) });

  for (let i = period + 1; i < candles.length; i++) {
    const change = candles[i].c - candles[i - 1].c;
    const gain = change >= 0 ? change : 0;
    const loss = change < 0 ? Math.abs(change) : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
    rsi = 100 - 100 / (1 + rs);
    points.push({ time: Math.floor(candles[i].t / 1000), value: +rsi.toFixed(2) });
  }
  return points;
}

/**
 * Compute ATR (Average True Range) from candles.
 */
export function computeAtr(
  candles: MockCandle[],
  period: number = 14,
): Array<{ time: number; value: number }> {
  if (candles.length < period + 1) return [];
  const points: Array<{ time: number; value: number }> = [];
  let atr = 0;

  // Seed: first ATR = average of first `period` true ranges
  for (let i = 1; i <= period; i++) {
    const tr = Math.max(
      candles[i].h - candles[i].l,
      Math.abs(candles[i].h - candles[i - 1].c),
      Math.abs(candles[i].l - candles[i - 1].c),
    );
    atr += tr;
  }
  atr /= period;
  points.push({ time: Math.floor(candles[period].t / 1000), value: +atr.toFixed(2) });

  for (let i = period + 1; i < candles.length; i++) {
    const tr = Math.max(
      candles[i].h - candles[i].l,
      Math.abs(candles[i].h - candles[i - 1].c),
      Math.abs(candles[i].l - candles[i - 1].c),
    );
    atr = (atr * (period - 1) + tr) / period;
    points.push({ time: Math.floor(candles[i].t / 1000), value: +atr.toFixed(2) });
  }
  return points;
}

function hashStr(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash) + str.charCodeAt(i);
    hash = hash & hash;
  }
  return Math.abs(hash);
}

/**
 * Build full indicator history from real candles.
 *
 * Computes EMA, SMA, BB, RSI, ATR from actual OHLC data.
 * For indicators that can't be computed on frontend (Stoch, ADX, MACD),
 * generates smooth approximation seeded from real current value.
 */
export function buildRealIndicatorHistory(
  candles: MockCandle[],
  slots: IndicatorSlot[],
  realIndicators: Record<string, number | null>,
): Record<string, Array<{ time: number; value: number }>> {
  const history: Record<string, Array<{ time: number; value: number }>> = {};

  for (const slot of slots) {
    const key = slot.key;
    const meta = slot.chart_meta;

    if (meta.pane === 'price_overlay') {
      if (key.includes('ema_9')) {
        history[key] = computeEmaFromCandles(candles, 9);
      } else if (key.includes('ema_21')) {
        history[key] = computeEmaFromCandles(candles, 21);
      } else if (key.includes('ema_50')) {
        history[key] = computeEmaFromCandles(candles, 50);
      } else if (key.includes('sma_20')) {
        history[key] = computeSmaFromCandles(candles, 20);
      } else if (key.includes('sma_50')) {
        history[key] = computeSmaFromCandles(candles, 50);
      } else if (key.includes('bbands_upper')) {
        const bb = computeBollingerBands(candles, 20, 2);
        history[key] = bb.upper;
      } else if (key.includes('bbands_lower')) {
        const bb = computeBollingerBands(candles, 20, 2);
        history[key] = bb.lower;
      } else if (key.includes('bbands_middle')) {
        const bb = computeBollingerBands(candles, 20, 2);
        history[key] = bb.middle;
      } else {
        const match = key.match(/_(\d+)$/);
        const period = match ? parseInt(match[1]) : 20;
        history[key] = computeEmaFromCandles(candles, period);
      }

      if (realIndicators[key] != null && !Number.isNaN(realIndicators[key]) && history[key] && history[key].length > 0) {
        history[key][history[key].length - 1].value = realIndicators[key] as number;
      }

    } else if (key.includes('rsi')) {
      const match = key.match(/rsi_(\d+)/);
      const period = match ? parseInt(match[1]) : 14;
      history[key] = computeRsi(candles, period);

      if (realIndicators[key] != null && !Number.isNaN(realIndicators[key]) && history[key] && history[key].length > 0) {
        history[key][history[key].length - 1].value = realIndicators[key] as number;
      }

    } else if (key.includes('atr')) {
      const match = key.match(/atr_(\d+)/);
      const period = match ? parseInt(match[1]) : 14;
      history[key] = computeAtr(candles, period);

      if (realIndicators[key] != null && !Number.isNaN(realIndicators[key]) && history[key] && history[key].length > 0) {
        history[key][history[key].length - 1].value = realIndicators[key] as number;
      }

    } else {
      const realVal = realIndicators[key];
      // Skip if null, NaN, or no candles — NaN from backend means indicator not yet computed
      if (realVal == null || Number.isNaN(realVal) || candles.length === 0) continue;

      const yRange = meta.y_range;
      const points: Array<{ time: number; value: number }> = [];

      for (let i = candles.length - 1; i >= 0; i--) {
        const noise = (Math.sin(i * 0.15 + hashStr(key) * 0.1) * 0.3 +
                       Math.sin(i * 0.07 + hashStr(key) * 0.2) * 0.2);
        const meanTarget = yRange
          ? (yRange[0] + yRange[1]) / 2
          : realVal as number;
        const blend = i / candles.length;
        const target = meanTarget * (1 - blend * 0.7) + (realVal as number) * blend * 0.7;
        const val = target + noise * (yRange ? (yRange[1] - yRange[0]) * 0.15 : Math.abs(realVal as number) * 0.2);

        if (yRange) {
          points.unshift({
            time: Math.floor(candles[i].t / 1000),
            value: +Math.max(yRange[0], Math.min(yRange[1], val)).toFixed(2),
          });
        } else {
          points.unshift({
            time: Math.floor(candles[i].t / 1000),
            value: +val.toFixed(4),
          });
        }
      }
      history[key] = points;
    }
  }

  return history;
}

// ── Color Themes ──

const LIGHT = {
  bg: 'transparent',
  text: '#6b7280',
  grid: 'rgba(209, 213, 219, 0.4)',
  gridBorder: 'rgba(209, 213, 219, 0.5)',
  upColor: '#22c55e',
  downColor: '#ef4444',
  volUp: 'rgba(34, 197, 94, 0.25)',
  volDown: 'rgba(239, 68, 68, 0.25)',
  priceBorder: 'rgba(209, 213, 219, 0.5)',
};

const DARK = {
  bg: 'transparent',
  text: '#9ca3af',
  grid: 'rgba(75, 85, 99, 0.2)',
  gridBorder: 'rgba(75, 85, 99, 0.3)',
  upColor: '#22c55e',
  downColor: '#ef4444',
  volUp: 'rgba(34, 197, 94, 0.15)',
  volDown: 'rgba(239, 68, 68, 0.15)',
  priceBorder: 'rgba(75, 85, 99, 0.3)',
};

// ── Sub-pane height config (base heights; expanded dynamically) ──

const SUB_PANE_HEIGHTS: Record<string, number> = {
  oscillator: 65,
  area: 60,
  histogram: 65,
};

/**
 * Calculate dynamic sub-pane heights.
 * When fewer pane types are present, the remaining ones expand
 * to fill the available vertical space.
 */
function getDynamicHeights(paneTypes: string[]): Record<string, number> {
  const maxHeight = 150; // max height per pane
  const baseTotal = Object.entries(SUB_PANE_HEIGHTS)
    .filter(([k]) => paneTypes.includes(k))
    .reduce((sum, [, v]) => sum + v, 0);

  // If we have fewer than 3 pane types, expand to fill
  const expandRatio = paneTypes.length < 3
    ? Math.min(maxHeight / Math.max(...Object.values(SUB_PANE_HEIGHTS)), 1.6)
    : 1;

  const heights: Record<string, number> = {};
  for (const pt of paneTypes) {
    heights[pt] = Math.round((SUB_PANE_HEIGHTS[pt] || 55) * expandRatio);
  }
  return heights;
}

// ── Main Renderer ──

/**
 * Initialize a timeframe card with multi-pane charts.
 *
 * Creates:
 *   1. Price pane (candlestick + price_overlay series + volume)
 *   2. Auto-created sub-panes for oscillator/area/histogram groups
 *
 * All driven by chart_meta from indicator slots — no names hardcoded.
 */
export function initMockupTimeframeCard(
  container: HTMLElement,
  candles: MockCandle[],
  slots: IndicatorSlot[],
  indicatorHistory: Record<string, Array<{ time: number; value: number }>>,
  options: MockupChartOptions,
): void {
  const containerId = options.symbol + '-' + options.timeframe;
  const theme = options.darkMode !== false ? DARK : LIGHT;

  // Destroy existing
  destroyMockupTimeframeCard(containerId);

  // Clear container
  container.innerHTML = '';

  // ── Step 1: Classify indicators by pane type ──
  const priceOverlays: IndicatorSlot[] = [];
  const subPaneSlots: Record<string, IndicatorSlot[]> = {};

  for (const slot of slots) {
    const meta = slot.chart_meta;
    if (meta.pane === 'price_overlay') {
      priceOverlays.push(slot);
    } else {
      if (!subPaneSlots[meta.pane]) subPaneSlots[meta.pane] = [];
      subPaneSlots[meta.pane].push(slot);
    }
  }

  // ── Step 2: Create DOM structure (dynamic pane sizing) ──
  const paneTypes = Object.keys(subPaneSlots);
  const dynamicHeights = getDynamicHeights(paneTypes);
  // Adjust price pane height based on how many sub-panes exist
  const priceHeight = paneTypes.length === 0 ? 240 : 195;

  const priceDiv = document.createElement('div');
  priceDiv.style.height = priceHeight + 'px';
  priceDiv.style.width = '100%';
  priceDiv.className = 'mockup-price-pane';
  container.appendChild(priceDiv);

  const subPaneDivs: Record<string, HTMLDivElement> = {};
  for (const [paneType] of Object.entries(subPaneSlots)) {
    const div = document.createElement('div');
    div.style.height = dynamicHeights[paneType] + 'px';
    div.style.width = '100%';
    div.className = `mockup-sub-pane mockup-${paneType}-pane`;
    container.appendChild(div);
    subPaneDivs[paneType] = div;
  }

  // ── Step 3: Create price pane chart (ZOOMABLE) ──
  const priceChart = createChart(priceDiv, {
    layout: {
      background: { type: ColorType.Solid, color: theme.bg },
      textColor: theme.text,
      fontSize: 10,
    },
    grid: {
      vertLines: { color: theme.grid },
      horzLines: { color: theme.grid },
    },
    autoSize: true,
    height: priceHeight,
    rightPriceScale: {
      borderColor: theme.priceBorder,
      scaleMargins: { top: 0.05, bottom: 0.25 },
    },
    timeScale: {
      timeVisible: ['1m', '5m', '15m'].includes(options.timeframe),
      secondsVisible: false,
      visible: true,
    },
    crosshair: { mode: 0 },
    handleScroll: true,
    handleScale: true,
  });

  // Candlestick series — reduced opacity to let indicators stand out
  const candleSeries = priceChart.addSeries(CandlestickSeries, {
    upColor: 'rgba(34, 197, 94, 0.5)',
    downColor: 'rgba(239, 68, 68, 0.5)',
    borderDownColor: 'rgba(239, 68, 68, 0.7)',
    borderUpColor: 'rgba(34, 197, 94, 0.7)',
    wickDownColor: 'rgba(239, 68, 68, 0.6)',
    wickUpColor: 'rgba(34, 197, 94, 0.6)',
  });

  const candleData = deduplicateByTime(candles.map((c) => ({
    time: Math.floor(c.t / 1000) as any,
    open: c.o,
    high: c.h,
    low: c.l,
    close: c.c,
  })));
  candleSeries.setData(candleData);

  // Volume overlay (bottom 15%)
  const volumeSeries = priceChart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
  });
  priceChart.priceScale('volume').applyOptions({
    scaleMargins: { top: 0.85, bottom: 0 },
  });
  const volumeData = deduplicateByTime(candles.map((c) => ({
    time: Math.floor(c.t / 1000) as any,
    value: c.v,
    color: c.c >= c.o ? theme.volUp : theme.volDown,
  })));
  volumeSeries.setData(volumeData);

  // Price overlay indicators (EMA, SMA, BB lines)
  for (const slot of priceOverlays) {
    const history = indicatorHistory[slot.key];
    if (!history || history.length === 0) continue;

    const lineSeries = priceChart.addSeries(LineSeries, {
      color: slot.chart_meta.color,
      lineWidth: (slot.chart_meta.line_width || 1.5) as any,
      lineStyle: slot.chart_meta.line_style || 0,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      priceScaleId: 'right',
    });
    const cleanData = sanitizeSeriesData(deduplicateByTime(history.map(p => ({ time: p.time as any, value: p.value }))));
    if (cleanData.length > 0) lineSeries.setData(cleanData);
  }

  // S/R levels as price lines
  if (options.srLevels) {
    if (options.srLevels.resistance) {
      candleSeries.createPriceLine({
        price: options.srLevels.resistance,
        color: 'rgba(239, 68, 68, 0.6)',
        lineWidth: 2,
        lineStyle: 0,
        axisLabelVisible: true,
        title: 'R',
      });
    }
    if (options.srLevels.support) {
      candleSeries.createPriceLine({
        price: options.srLevels.support,
        color: 'rgba(34, 197, 94, 0.6)',
        lineWidth: 2,
        lineStyle: 0,
        axisLabelVisible: true,
        title: 'S',
      });
    }
  }

  priceChart.timeScale().fitContent();

  // ── Step 4: Create sub-pane charts ──
  const subPaneCharts: Record<string, any> = {};

  for (const [paneType, paneSlots] of Object.entries(subPaneSlots)) {
    const div = subPaneDivs[paneType];
    const height = dynamicHeights[paneType] || 55;

    const subChart = createChart(div, {
      layout: {
        background: { type: ColorType.Solid, color: theme.bg },
        textColor: theme.text,
        fontSize: 9,
      },
      grid: {
        vertLines: { color: theme.grid },
        horzLines: { color: theme.grid },
      },
      autoSize: true,
      height,
      rightPriceScale: {
        borderColor: theme.priceBorder,
        scaleMargins: { top: 0.05, bottom: 0.05 },
      },
      leftPriceScale: { visible: false },
      timeScale: { timeVisible: false, visible: false },
      crosshair: { mode: 0 },
      handleScroll: false,
      handleScale: false,
    });

    for (const slot of paneSlots) {
      const history = indicatorHistory[slot.key];
      if (!history || history.length === 0) continue;
      const meta = slot.chart_meta;

      if (meta.series === 'histogram') {
        const histSeries = subChart.addSeries(HistogramSeries, {
          color: meta.color,
          priceLineVisible: false,
          lastValueVisible: false,
          priceScaleId: 'right',
        });
        const histData = sanitizeSeriesData(history.map((p) => ({
          time: p.time as any,
          value: p.value,
          color: p.value >= 0 ? 'rgba(34, 197, 94, 0.6)' : 'rgba(239, 68, 68, 0.6)',
        }))).map(p => ({ time: p.time, value: p.value, color: (p as any).color }));
        if (histData.length > 0) histSeries.setData(deduplicateByTime(histData));

      } else if (meta.series === 'area') {
        const areaSeries = subChart.addSeries(AreaSeries, {
          topColor: meta.color + '40',
          bottomColor: meta.color + '05',
          lineColor: meta.color,
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          priceScaleId: 'right',
        });
        const cleanArea = sanitizeSeriesData(deduplicateByTime(history.map(p => ({ time: p.time as any, value: p.value }))));
        if (cleanArea.length > 0) areaSeries.setData(cleanArea);

      } else {
        const lineSeries = subChart.addSeries(LineSeries, {
          color: meta.color,
          lineWidth: (meta.line_width || 1.5) as any,
          lineStyle: meta.line_style || 0,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          priceScaleId: 'right',
        });
        const cleanLine = sanitizeSeriesData(deduplicateByTime(history.map(p => ({ time: p.time as any, value: p.value }))));
        if (cleanLine.length > 0) lineSeries.setData(cleanLine);

        if (meta.ref_lines.length > 0 && subPaneCharts[paneType + '_refs_added']) {
          // skip
        } else if (meta.ref_lines.length > 0) {
          for (const refVal of meta.ref_lines) {
            lineSeries.createPriceLine({
              price: refVal,
              color: 'rgba(107, 114, 128, 0.35)',
              lineWidth: 1,
              lineStyle: 1,
              axisLabelVisible: false,
              title: '',
            });
          }
          subPaneCharts[paneType + '_refs_added'] = true;
        }
      }
    }

    subChart.timeScale().fitContent();

    // One-way sync: zooming/scrolling the price chart updates this sub-pane.
    // No reverse sync — prevents circular callback loops that block the event loop.
    priceChart.timeScale().subscribeVisibleLogicalRangeChange((range: any) => {
      if (range) {
        try { subChart.timeScale().setVisibleLogicalRange(range); } catch (e) { /* ignore */ }
      }
    });

    subPaneCharts[paneType] = subChart;
  }

  // ── Step 5: Store for cleanup ──
  chartGroups[containerId] = {
    priceChart,
    subPanes: subPaneCharts,
  };
}

export function destroyMockupTimeframeCard(containerId: string): void {
  const group = chartGroups[containerId];
  if (!group) return;
  try { group.priceChart.remove(); } catch (e) { /* ignore */ }
  for (const subChart of Object.values(group.subPanes)) {
    try { (subChart as any).remove(); } catch (e) { /* ignore */ }
  }
  delete chartGroups[containerId];
}

export function destroyAllMockupCharts(): void {
  for (const id of Object.keys(chartGroups)) {
    destroyMockupTimeframeCard(id);
  }
}

// ═══════════════════════════════════════════════════════
//  MOCKUP CARD INITIALIZATION
// ═══════════════════════════════════════════════════════

export function initMockupCard(
  el: HTMLElement,
  asset: any,
  scope: { initialized: boolean; loading: boolean; [key: string]: any },
): void {
  let done = false;

  const doInit = async () => {
    if (done) return;
    done = true;

    const a = asset;
    const symbol = a.symbol;
    const broker = (window as any)._activeBroker || 'binance';
    const config = (window as any)._mockConfig;

    if (!config || !symbol) {
      console.warn('[Mockup] No config or symbol');
      scope.initialized = true;
      return;
    }

    scope.loading = true;
    const darkMode = document.documentElement.classList.contains('dark');
    console.log('[Mockup] Fetching real klines for', symbol, 'broker:', broker);

    // Step 1: Fetch real kline candles for all timeframes
    const klineData: Record<string, MockCandle[]> = {};
    if ((window as any)._fetchKlines) {
      try {
        const result = await (window as any)._fetchKlines(symbol, config.timeframes, broker, 100);
        Object.assign(klineData, result);
      } catch (e) {
        console.error('[Mockup] Kline fetch failed:', e);
      }
    }

    // Step 2: Build indicator history
    const realIndicators: Record<string, number | null> = (a.indicators) || {};
    const allHistory: Record<string, Record<string, Array<{ time: number; value: number }>>> = {};

    for (const tf of config.timeframes) {
      const candles = klineData[tf.key] || [];
      const slots: IndicatorSlot[] = config.indicators[tf.key] || [];

      if (candles.length > 0 && (window as any)._buildIndicatorHistory) {
        allHistory[tf.key] = (window as any)._buildIndicatorHistory(candles, slots, realIndicators);
      }

      // Step 3: Init chart for this timeframe
      const containerId = 'mockup-tf-' + symbol + '-' + tf.key;
      const container = document.getElementById(containerId);
      if (!container) {
        console.warn('[Mockup] Container not found:', containerId);
        continue;
      }

      let tfCandles = klineData[tf.key];
      if (!tfCandles || tfCandles.length === 0) {
        continue;
      }

      if ((window as any)._initMockupTF) {
        try {
          (window as any)._initMockupTF(container, tfCandles, slots, allHistory[tf.key] || {}, {
            symbol: symbol,
            timeframe: tf.key,
            darkMode: darkMode,
            srLevels: {
              support: a.vars ? a.vars.support : null,
              resistance: a.vars ? a.vars.resistance : null,
            },
          });
        } catch (e) {
          console.error('[Mockup] Chart error for', tf.key, e);
        }
      }
    }

    scope.loading = false;
    scope.initialized = true;

    // ── Auto-update: refresh klines every 30s to keep charts live ──
    const refreshInterval = setInterval(async () => {
      if (!config || !symbol) return;
      try {
        const freshKlines: Record<string, MockCandle[]> = {};
        if ((window as any)._fetchKlines) {
          const result = await (window as any)._fetchKlines(symbol, config.timeframes, broker, 100);
          Object.assign(freshKlines, result);
        }

        const freshRealIndicators: Record<string, number | null> = a.indicators || {};
        for (const tf of config.timeframes) {
          const freshCandles = freshKlines[tf.key];
          if (!freshCandles || freshCandles.length === 0) continue;

          const slots: IndicatorSlot[] = config.indicators[tf.key] || [];
          const containerId = 'mockup-tf-' + symbol + '-' + tf.key;
          const container = document.getElementById(containerId);
          if (!container) continue;

          let freshHistory: Record<string, Array<{ time: number; value: number }>> = {};
          if ((window as any)._buildIndicatorHistory) {
            freshHistory = (window as any)._buildIndicatorHistory(freshCandles, slots, freshRealIndicators);
          }

          if ((window as any)._initMockupTF) {
            try {
              (window as any)._initMockupTF(container, freshCandles, slots, freshHistory || {}, {
                symbol,
                timeframe: tf.key,
                darkMode,
                srLevels: {
                  support: a.vars ? a.vars.support : null,
                  resistance: a.vars ? a.vars.resistance : null,
                },
              });
            } catch (e) {
              console.error('[Mockup] Chart refresh error for', tf.key, e);
            }
          }
        }
      } catch (e) {
        console.error('[Mockup] Auto-refresh error:', e);
      }
    }, 30_000); // every 30 seconds

    // Store interval for cleanup
    el._mockupRefreshInterval = refreshInterval;
  };

  const handler = () => { doInit(); };
  window.addEventListener('mockup-init-charts', handler);

  // Store cleanup on element
  el._mockupCleanup = () => {
    window.removeEventListener('mockup-init-charts', handler);
    if (el._mockupRefreshInterval) {
      clearInterval(el._mockupRefreshInterval);
    }
  };
}

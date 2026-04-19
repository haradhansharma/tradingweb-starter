/**
 * Chart Renderer — Generic Multi-Pane Chart System
 * =======================================================
 * Creates a price pane + auto-generated sub-panes for oscillators,
 * areas, and histograms — all driven by chart_meta from config.
 *
 * No indicator names are hardcoded. The renderer reads chart_meta
 * from each indicator slot and places it on the correct pane.
 *
 * Architecture:
 *   ALL indicator computation happens server-side (pandas-ta).
 *   The frontend just fetches pre-computed { time, value } series
 *   and feeds them directly into Lightweight Charts. Zero computation.
 *
 * Usage:
 *   initTimeframeCard(container, candles, indicatorSlots, indicatorHistory, options)
 *   destroyTimeframeCard(containerId)
 *
 * Data flow:
 *   fetchKlinesForTimeframes() → fetches candles + indicator series (parallel)
 *   buildRealIndicatorHistory() → maps backend series to per-slot format
 *   initTimeframeCard() → plots everything
 */

import {
  createChart, ColorType,
  AreaSeries, CandlestickSeries, HistogramSeries, LineSeries,
} from 'lightweight-charts';

// ═══════════════════════════════════════════════════════
//  CHART CONFIGURATION — single source of truth
// ═══════════════════════════════════════════════════════
//  Change CANDLE_COUNT here to adjust ALL timeframes at once.
//  Both klines and indicator-series endpoints use this value.
// ═══════════════════════════════════════════════════════

/** Number of candles to display on every timeframe card. */
const CANDLE_COUNT = 100;

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

export interface ChartOptions {
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
//  DATA FETCHING
// ═══════════════════════════════════════════════════════
// All indicator computation is done server-side by pandas-ta.
// The frontend just fetches pre-computed { time, value } series
// and feeds them directly into Lightweight Charts.

/**
 * Fetch kline candles + indicator series from REST API.
 *
 * Klines:  one request per timeframe (parallel)
 * Series:  single request for ALL timeframes/indicators at once
 *
 * Returns { [timeframe]: MockCandle[] }
 * Side-effect: stores indicator series on window._indicatorSeries
 */
export async function fetchKlinesForTimeframes(
  symbol: string,
  timeframes: Array<{ key: string }>,
  broker: string,
  limit: number = CANDLE_COUNT,
): Promise<Record<string, MockCandle[]>> {
  const results: Record<string, MockCandle[]> = {};
  const brokerParam = broker || 'binance';

  // ── Fetch candles per TF (parallel) ──
  const candlePromises = timeframes.map(async (tf) => {
    try {
      const resp = await fetch(
        `/api/market/klines/${symbol}?interval=${tf.key}&limit=${limit}&broker=${brokerParam}`
      );
      if (!resp.ok) return;
      const data = await resp.json();
      const candles: MockCandle[] = (data.candles || []).map((c: any) => ({
        t: c.t, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v,
      }));
      if (candles.length > 0) results[tf.key] = candles;
    } catch (e) {
      console.error(`[Chart] Kline fetch error for ${tf.key}:`, e);
    }
  });

  // ── Fetch indicator series (single request, all TFs) ──
  const seriesPromise = (async () => {
    try {
      const resp = await fetch(
        `/api/market/indicator-series/${symbol}?broker=${brokerParam}&limit=${limit}`
      );
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.series) {
        (window as any)._indicatorSeries = data.series;
      }
    } catch (e) {
      console.error('[Chart] Indicator series fetch error:', e);
    }
  })();

  await Promise.all([...candlePromises, seriesPromise]);
  return results;
}

/**
 * Build indicator history from backend-computed series data.
 *
 * All indicator computation happens server-side (pandas-ta).
 * This function just maps the backend's { time, value } arrays
 * to the expected format, filtered per-slot.
 *
 * Returns empty if backend series not yet available (chart will
 * refresh on next 30s cycle).
 */
export function buildRealIndicatorHistory(
  _candles: MockCandle[],
  slots: IndicatorSlot[],
  _realIndicators: Record<string, number | null>,
): Record<string, Array<{ time: number; value: number }>> {
  const history: Record<string, Array<{ time: number; value: number }>> = {};
  const series: Record<string, Array<{ time: number; value: number }>> | undefined =
    (window as any)._indicatorSeries;

  if (!series) return history;

  for (const slot of slots) {
    const backendData = series[slot.key];
    if (backendData && backendData.length > 0) {
      history[slot.key] = backendData.map((p: any) => ({
        time: p.time,
        value: p.value,
      }));
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
export function initTimeframeCard(
  container: HTMLElement,
  candles: MockCandle[],
  slots: IndicatorSlot[],
  indicatorHistory: Record<string, Array<{ time: number; value: number }>>,
  options: ChartOptions,
): void {
  const containerId = options.symbol + '-' + options.timeframe;
  const theme = options.darkMode !== false ? DARK : LIGHT;

  // Destroy existing
  destroyTimeframeCard(containerId);

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
  priceDiv.className = 'price-pane';
  container.appendChild(priceDiv);

  const subPaneDivs: Record<string, HTMLDivElement> = {};
  for (const [paneType] of Object.entries(subPaneSlots)) {
    const div = document.createElement('div');
    div.style.height = dynamicHeights[paneType] + 'px';
    div.style.width = '100%';
    div.className = `sub-pane ${paneType}-pane`;
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

export function destroyTimeframeCard(containerId: string): void {
  const group = chartGroups[containerId];
  if (!group) return;
  try { group.priceChart.remove(); } catch (e) { /* ignore */ }
  for (const subChart of Object.values(group.subPanes)) {
    try { (subChart as any).remove(); } catch (e) { /* ignore */ }
  }
  delete chartGroups[containerId];
}

export function destroyAllCharts(): void {
  for (const id of Object.keys(chartGroups)) {
    destroyTimeframeCard(id);
  }
}

// ═══════════════════════════════════════════════════════
//  CARD INITIALIZATION
// ═══════════════════════════════════════════════════════

export function initCard(
  el: HTMLElement,
  asset: any,
  scope: { initialized: boolean; loading: boolean; [key: string]: any },
): void {
  let done = false;

  const doInit = async () => {
    if (done) return;

    const a = asset;
    const symbol = a.symbol;
    const broker = (window as any)._activeBroker || 'binance';
    const config = (window as any)._indicatorConfig;

    if (!config || !symbol) {
      // Config not yet loaded — don't mark done, wait for init-charts event
      console.log('[Chart] Config not ready, waiting for indicator config...');
      return;
    }

    // Mark done only after confirming config is available
    done = true;

    scope.loading = true;
    const darkMode = document.documentElement.classList.contains('dark');
    console.log('[Chart] Fetching real klines for', symbol, 'broker:', broker);

    // Step 1: Fetch real kline candles for all timeframes
    const klineData: Record<string, MockCandle[]> = {};
    if ((window as any)._fetchKlines) {
      try {
        const result = await (window as any)._fetchKlines(symbol, config.timeframes, broker, CANDLE_COUNT);
        Object.assign(klineData, result);
      } catch (e) {
        console.error('[Chart] Kline fetch failed:', e);
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
      const containerId = 'tf-' + symbol + '-' + tf.key;
      const container = document.getElementById(containerId);
      if (!container) {
        console.warn('[Chart] Container not found:', containerId);
        continue;
      }

      let tfCandles = klineData[tf.key];
      if (!tfCandles || tfCandles.length === 0) {
        continue;
      }

      if ((window as any)._initTF) {
        try {
          (window as any)._initTF(container, tfCandles, slots, allHistory[tf.key] || {}, {
            symbol: symbol,
            timeframe: tf.key,
            darkMode: darkMode,
            srLevels: {
              support: a.vars ? a.vars.support : null,
              resistance: a.vars ? a.vars.resistance : null,
            },
          });
        } catch (e) {
          console.error('[Chart] Chart error for', tf.key, e);
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
          const result = await (window as any)._fetchKlines(symbol, config.timeframes, broker, CANDLE_COUNT);
          Object.assign(freshKlines, result);
        }

        const freshRealIndicators: Record<string, number | null> = a.indicators || {};
        for (const tf of config.timeframes) {
          const freshCandles = freshKlines[tf.key];
          if (!freshCandles || freshCandles.length === 0) continue;

          const slots: IndicatorSlot[] = config.indicators[tf.key] || [];
          const containerId = 'tf-' + symbol + '-' + tf.key;
          const container = document.getElementById(containerId);
          if (!container) continue;

          let freshHistory: Record<string, Array<{ time: number; value: number }>> = {};
          if ((window as any)._buildIndicatorHistory) {
            freshHistory = (window as any)._buildIndicatorHistory(freshCandles, slots, freshRealIndicators);
          }

          if ((window as any)._initTF) {
            try {
              (window as any)._initTF(container, freshCandles, slots, freshHistory || {}, {
                symbol,
                timeframe: tf.key,
                darkMode,
                srLevels: {
                  support: a.vars ? a.vars.support : null,
                  resistance: a.vars ? a.vars.resistance : null,
                },
              });
            } catch (e) {
              console.error('[Chart] Chart refresh error for', tf.key, e);
            }
          }
        }
      } catch (e) {
        console.error('[Chart] Auto-refresh error:', e);
      }
    }, 30_000); // every 30 seconds

    // Store interval for cleanup
    el._refreshInterval = refreshInterval;
  };

  const handler = () => { doInit(); };
  window.addEventListener('init-charts', handler);

  // Fire immediately — don't wait for an event that may never come
  doInit();

  // Store cleanup on element
  el._cleanup = () => {
    window.removeEventListener('init-charts', handler);
    if (el._refreshInterval) {
      clearInterval(el._refreshInterval);
    }
  };
}

/**
 * Kline Chart Utility — TradingView Lightweight Charts Wrapper
 * =============================================================
 * Fetches candlestick data from the backend REST endpoint and
 * renders interactive charts with volume overlay and indicator annotations.
 *
 * Chart Types:
 *   - Micro Chart:  Compact 80px area chart for card headers (25h trend)
 *   - Full Chart:   Candlestick + Volume + EMA/BB/SR overlays (expanded panel)
 *
 * Usage (from Alpine store):
 *   initMicroChart(id, symbol, broker)          → header micro chart
 *   initKlineChart(id, symbol, broker, options)  → expanded chart with overlays
 *   destroyMicroChart(id)                        → cleanup
 *   destroyChart(id)                             → cleanup
 *
 * Lightweight Charts v5 API:
 *   chart.addSeries(CandlestickSeries, options)
 *   chart.addSeries(AreaSeries, options)
 *   chart.addSeries(HistogramSeries, options)
 *   series.createPriceLine(options)
 */

import {
  createChart, ColorType,
  AreaSeries, CandlestickSeries, HistogramSeries, LineSeries,
} from 'lightweight-charts';

// Store chart instances for cleanup
const chartInstances: Record<string, any> = {};

// ═══════════════════════════════════════════════
//  MICRO CHART — Compact area chart for card header
// ═══════════════════════════════════════════════

/**
 * Initialize a compact area chart for the card header.
 * Fetches 100 candles of 15m data (~25h trend) and renders
 * as a gradient-filled area chart with no axes or controls.
 */
export async function initMicroChart(containerId: string, symbol: string, broker: string = 'binance'): Promise<void> {
  const container = document.getElementById(containerId);
  if (!container) return;

  // Destroy existing chart if any
  if (chartInstances[containerId]) {
    chartInstances[containerId].remove();
    delete chartInstances[containerId];
  }

  // Fetch kline data — 100 candles of 15m for ~25h trend
  try {
    const resp = await fetch(`/api/market/klines/${symbol}?interval=15m&limit=100&broker=${broker}`);
    if (!resp.ok) return;
    const data = await resp.json();
    const candles = data.candles;
    if (!candles || candles.length === 0) return;

    // Determine trend color from first vs last close
    const firstClose = candles[0].c;
    const lastClose = candles[candles.length - 1].c;
    const isBullish = lastClose >= firstClose;
    const lineColor = isBullish ? '#22c55e' : '#ef4444';
    const topColor = isBullish ? 'rgba(34, 197, 94, 0.30)' : 'rgba(239, 68, 68, 0.30)';
    const bottomColor = isBullish ? 'rgba(34, 197, 94, 0.02)' : 'rgba(239, 68, 68, 0.02)';

    const chart = createChart(container, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: 'transparent',
        fontSize: 10,
      },
      grid: {
        vertLines: { visible: false },
        horzLines: { visible: false },
      },
      width: container.clientWidth,
      height: 80,
      rightPriceScale: { visible: false },
      timeScale: { visible: false, fixLeftEdge: true, fixRightEdge: true },
      crosshair: { mode: 0 },
      handleScroll: false,
      handleScale: false,
    });

    const areaSeries = chart.addSeries(AreaSeries, {
      topColor,
      bottomColor,
      lineColor,
      lineWidth: 1.5,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false,
    });

    areaSeries.setData(candles.map((c: any) => ({
      time: Math.floor(c.t / 1000) as any,
      value: c.c,
    })));

    chart.timeScale().fitContent();
    chartInstances[containerId] = chart;

    // Handle resize
    const resizeObserver = new ResizeObserver(() => {
      if (chartInstances[containerId]) {
        chart.applyOptions({ width: container.clientWidth });
      }
    });
    resizeObserver.observe(container);
    (container as any)._resizeObserver = resizeObserver;

  } catch (e) {
    console.error(`[MicroChart] Failed to init for ${symbol}:`, e);
  }
}

/**
 * Destroy a micro chart instance and clean up resize observer.
 */
export function destroyMicroChart(containerId: string): void {
  const container = document.getElementById(containerId);
  if (chartInstances[containerId]) {
    chartInstances[containerId].remove();
    delete chartInstances[containerId];
  }
  if (container && (container as any)._resizeObserver) {
    (container as any)._resizeObserver.disconnect();
  }
}

// ═══════════════════════════════════════════════
//  FULL CHART — Candlestick + Volume + Overlays
// ═══════════════════════════════════════════════

/**
 * Options for the full kline chart.
 */
export interface ChartOptions {
  /** Candle interval (default: '1h'). Supported: 1m, 5m, 15m, 1h, 4h, 1d */
  interval?: string;
  /** Number of candles to fetch (default: 200, max: 1500) */
  limit?: number;
  /** Indicator values from backend — key format: "{tf}_{indicator}_{param}" */
  indicators?: Record<string, number | null>;
  /** S/R levels to display as price lines on the chart */
  srLevels?: {
    support?: number | null;
    resistance?: number | null;
    support_secondary?: number | null;
    resistance_secondary?: number | null;
  };
}

/**
 * Initialize the full candlestick chart for the expanded panel.
 * Renders candlesticks + volume histogram, then overlays EMA/BB/SR
 * lines from backend indicator values.
 */
export async function initKlineChart(
  containerId: string,
  symbol: string,
  broker: string = 'binance',
  options?: ChartOptions,
): Promise<void> {
  const container = document.getElementById(containerId);
  if (!container) return;

  const interval = options?.interval || '1h';
  const limit = options?.limit || 200;
  const indicators = options?.indicators || {};
  const srLevels = options?.srLevels || {};

  // Destroy existing chart if any
  if (chartInstances[containerId]) {
    chartInstances[containerId].remove();
    delete chartInstances[containerId];
  }

  // Fetch kline data
  try {
    const resp = await fetch(`/api/market/klines/${symbol}?interval=${interval}&limit=${limit}&broker=${broker}`);
    if (!resp.ok) return;
    const data = await resp.json();
    const candles = data.candles;
    if (!candles || candles.length === 0) return;

    const chart = createChart(container, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#9ca3af',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(75, 85, 99, 0.15)' },
        horzLines: { color: 'rgba(75, 85, 99, 0.15)' },
      },
      width: container.clientWidth,
      height: 320,
      timeScale: {
        timeVisible: interval === '1m' || interval === '5m' || interval === '15m',
        secondsVisible: false,
      },
      crosshair: {
        mode: 0,
      },
      rightPriceScale: {
        borderColor: 'rgba(75, 85, 99, 0.3)',
      },
    });

    // ── Candlestick Series ──
    const candlestickSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderDownColor: '#ef4444',
      borderUpColor: '#22c55e',
      wickDownColor: '#ef4444',
      wickUpColor: '#22c55e',
    });

    candlestickSeries.setData(candles.map((c: any) => ({
      time: Math.floor(c.t / 1000) as any,
      open: c.o,
      high: c.h,
      low: c.l,
      close: c.c,
    })));

    // ── Volume Series (bottom 15%) ──
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });

    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
    });

    volumeSeries.setData(candles.map((c: any) => ({
      time: Math.floor(c.t / 1000) as any,
      value: c.v,
      color: c.c >= c.o ? 'rgba(34, 197, 94, 0.25)' : 'rgba(239, 68, 68, 0.25)',
    })));

    // ── EMA Overlay Lines (current values as horizontal price lines) ──
    const ema9 = indicators[`${interval}_ema_9`];
    const ema21 = indicators[`${interval}_ema_21`];

    if (ema9 !== null && ema9 !== undefined && ema9 > 0) {
      candlestickSeries.createPriceLine({
        price: ema9,
        color: 'rgba(34, 211, 238, 0.7)',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: `EMA9`,
      });
    }

    if (ema21 !== null && ema21 !== undefined && ema21 > 0) {
      candlestickSeries.createPriceLine({
        price: ema21,
        color: 'rgba(56, 189, 248, 0.7)',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: `EMA21`,
      });
    }

    // ── Bollinger Bands (current values as horizontal price lines) ──
    const bbUpper = indicators[`${interval}_bbands_upper`];
    const bbLower = indicators[`${interval}_bbands_lower`];
    const bbMid = indicators[`${interval}_bbands_middle`];

    if (bbUpper && bbLower && bbMid) {
      candlestickSeries.createPriceLine({
        price: bbUpper,
        color: 'rgba(168, 85, 247, 0.4)',
        lineWidth: 1,
        lineStyle: 1,
        axisLabelVisible: true,
        title: 'BB Upper',
      });
      candlestickSeries.createPriceLine({
        price: bbMid,
        color: 'rgba(168, 85, 247, 0.25)',
        lineWidth: 1,
        lineStyle: 1,
        axisLabelVisible: true,
        title: 'BB Mid',
      });
      candlestickSeries.createPriceLine({
        price: bbLower,
        color: 'rgba(168, 85, 247, 0.4)',
        lineWidth: 1,
        lineStyle: 1,
        axisLabelVisible: true,
        title: 'BB Lower',
      });
    }

    // ── S/R Level Price Lines ──
    if (srLevels.resistance) {
      candlestickSeries.createPriceLine({
        price: srLevels.resistance,
        color: 'rgba(239, 68, 68, 0.6)',
        lineWidth: 2,
        lineStyle: 0,
        axisLabelVisible: true,
        title: 'Resistance',
      });
    }
    if (srLevels.support) {
      candlestickSeries.createPriceLine({
        price: srLevels.support,
        color: 'rgba(34, 197, 94, 0.6)',
        lineWidth: 2,
        lineStyle: 0,
        axisLabelVisible: true,
        title: 'Support',
      });
    }
    if (srLevels.resistance_secondary) {
      candlestickSeries.createPriceLine({
        price: srLevels.resistance_secondary,
        color: 'rgba(239, 68, 68, 0.35)',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: 'R2',
      });
    }
    if (srLevels.support_secondary) {
      candlestickSeries.createPriceLine({
        price: srLevels.support_secondary,
        color: 'rgba(34, 197, 94, 0.35)',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: 'S2',
      });
    }

    chart.timeScale().fitContent();
    chartInstances[containerId] = chart;

    // Handle resize
    const resizeObserver = new ResizeObserver(() => {
      if (chartInstances[containerId]) {
        chart.applyOptions({ width: container.clientWidth });
      }
    });
    resizeObserver.observe(container);
    (container as any)._resizeObserver = resizeObserver;

  } catch (e) {
    console.error(`[Chart] Failed to init chart for ${symbol}:`, e);
  }
}

/**
 * Destroy a full chart instance and clean up resize observer.
 */
export function destroyChart(containerId: string): void {
  const container = document.getElementById(containerId);
  if (chartInstances[containerId]) {
    chartInstances[containerId].remove();
    delete chartInstances[containerId];
  }
  if (container && (container as any)._resizeObserver) {
    (container as any)._resizeObserver.disconnect();
  }
}

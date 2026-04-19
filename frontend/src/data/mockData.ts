/**
 * Mock Data for AssetCardMockup
 * ==============================
 * Generates realistic sample data matching the exact shapes of:
 *   - REST API: GET /market/klines/{symbol}
 *   - WS data: indicators payload
 *   - REST API: GET /market/indicator-config (extended with chart_meta)
 *
 * All data is synthetic — no backend calls needed.
 * When switching to production, only the data source changes —
 * chart rendering code stays identical.
 */

// ── Types ──

export interface MockCandle {
  t: number;   // timestamp ms
  o: number;   // open
  h: number;   // high
  l: number;   // low
  c: number;   // close
  v: number;   // volume
}

export interface ChartMeta {
  /** Which pane this indicator belongs to */
  pane: 'price_overlay' | 'oscillator' | 'area' | 'histogram';
  /** Series type: line, histogram, area */
  series: 'line' | 'histogram' | 'area';
  /** Fixed Y-axis range [min, max], null = auto scale */
  y_range: [number, number] | null;
  /** Horizontal reference lines (e.g. RSI 30/70, ADX 25) */
  ref_lines: number[];
  /** Line/area color */
  color: string;
  /** Line style: 0=solid, 1=dotted, 2=dashed */
  line_style?: number;
  /** Line width */
  line_width?: number;
}

export interface IndicatorSlot {
  key: string;
  label: string;
  format: 'decimal' | 'price' | 'signed' | 'volume';
  decimals: number;
  bold: boolean;
  font_size: string;
  color_rules: Array<{ op: string; value: number; class: string }>;
  default_class: string;
  chart_meta: ChartMeta;
}

export interface TimeframeConfig {
  key: string;
  label: string;
  color: string;
}

export interface MockIndicatorConfig {
  timeframes: TimeframeConfig[];
  grid_columns: number;
  indicators: Record<string, IndicatorSlot[]>;
}

// ── Helper: seeded random for reproducible data ──

function seededRandom(seed: number): () => number {
  let s = seed;
  return () => {
    s = (s * 16807 + 0) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

// ── Candle Generator ──

export function generateCandles(
  symbol: string,
  count: number,
  interval: string,
  basePrice: number = 97000,
): MockCandle[] {
  const rand = seededRandom(symbol.charCodeAt(0) * 1000 + interval.charCodeAt(0) * 100);
  const candles: MockCandle[] = [];

  // Interval to ms
  const intervalMs: Record<string, number> = {
    '1m': 60_000, '5m': 300_000, '15m': 900_000,
    '30m': 1_800_000, '1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000,
  };
  const step = intervalMs[interval] || 3_600_000;

  const now = Date.now();
  let price = basePrice;
  // Start from 'count' intervals ago
  const startTime = now - (count * step);

  for (let i = 0; i < count; i++) {
    const t = startTime + i * step;
    const volatility = price * (0.001 + rand() * 0.003); // 0.1% - 0.4%
    const drift = (rand() - 0.48) * volatility; // slight upward bias
    const open = price;
    const close = price + drift;
    const high = Math.max(open, close) + rand() * volatility * 0.5;
    const low = Math.min(open, close) - rand() * volatility * 0.5;
    const volume = (500 + rand() * 5000) * (symbol === 'BTCUSDT' ? 10 : 3);
    candles.push({
      t: Math.floor(t / 1000) * 1000, // align to interval
      o: +open.toFixed(2),
      h: +high.toFixed(2),
      l: +low.toFixed(2),
      c: +close.toFixed(2),
      v: Math.floor(volume),
    });
    price = close;
  }

  return candles;
}

// ── Indicator History Generator ──

export function generateIndicatorHistory(
  key: string,
  count: number,
  candles: MockCandle[],
  meta: ChartMeta,
): Array<{ time: number; value: number }> {
  const rand = seededRandom(hashString(key) + candles.length);
  const points: Array<{ time: number; value: number }> = [];
  const prices = candles.map(c => c.c);

  for (let i = 0; i < count && i < candles.length; i++) {
    let value: number;

    if (meta.pane === 'price_overlay') {
      // Price-following indicators (EMA, SMA, BB)
      // Smooth lagging version of price
      const lookback = Math.min(i + 1, 20);
      const slice = prices.slice(Math.max(0, i - lookback + 1), i + 1);
      const avg = slice.reduce((a, b) => a + b, 0) / slice.length;
      const offset = (rand() - 0.5) * avg * 0.005;
      value = avg + offset;

      // BB: key contains "upper" or "lower" — add spread
      if (key.includes('bbands_upper')) {
        value = avg + avg * (0.005 + rand() * 0.015);
      } else if (key.includes('bbands_lower')) {
        value = avg - avg * (0.005 + rand() * 0.015);
      } else if (key.includes('bbands_middle')) {
        value = avg;
      }
    } else if (meta.y_range) {
      // Fixed range oscillators (RSI, Stoch, ADX)
      const [min, max] = meta.y_range;
      const mid = (min + max) / 2;
      const amplitude = (max - min) / 2;

      // Mean-reverting random walk
      let base = mid + Math.sin(i * 0.05 + hashString(key) * 0.1) * amplitude * 0.6;
      // Add noise
      value = base + (rand() - 0.5) * amplitude * 0.3;
      value = Math.max(min, Math.min(max, value));

      // Stoch D is smoother version of K
      if (key.includes('stoch_d')) {
        const kIdx = i - 1;
        if (kIdx >= 0 && points[kIdx]) {
          value = points[kIdx].value * 0.6 + value * 0.4;
        }
      }
      // ADX dmp/dmn
      if (key.includes('adx_dmp') || key.includes('dmp')) {
        value = mid + (rand() - 0.4) * amplitude * 0.5;
      }
      if (key.includes('adx_dmn') || key.includes('dmn')) {
        value = mid + (rand() - 0.6) * amplitude * 0.5;
      }
    } else {
      // Dynamic range (ATR, MACD)
      if (key.includes('atr')) {
        // ATR based on recent candle ranges
        const lookback = Math.min(i + 1, 14);
        let sumRange = 0;
        for (let j = Math.max(0, i - lookback + 1); j <= i; j++) {
          sumRange += candles[j].h - candles[j].l;
        }
        value = sumRange / lookback;
      } else if (key.includes('macd_hist')) {
        // MACD histogram oscillates around 0
        value = (rand() - 0.48) * (key.includes('1h') ? 50 : key.includes('4h') ? 80 : 20);
      } else if (key.includes('macd_signal')) {
        // MACD signal line — smoother
        let base = Math.sin(i * 0.03 + hashString(key) * 0.2) * 30;
        value = base + (rand() - 0.5) * 10;
      } else {
        // MACD line
        let base = Math.sin(i * 0.03 + hashString(key) * 0.15) * 40;
        value = base + (rand() - 0.5) * 15;
      }
    }

    points.push({
      time: Math.floor(candles[i].t / 1000) as number,
      value: +value.toFixed(4),
    });
  }

  return points;
}

function hashString(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = ((hash << 5) - hash) + char;
    hash = hash & hash; // Convert to 32-bit integer
  }
  return Math.abs(hash);
}

// ── Extended Indicator Config with chart_meta ──

export function getMockIndicatorConfig(): MockIndicatorConfig {
  const timeframes: TimeframeConfig[] = [
    { key: '1m', label: '1m', color: 'sky' },
    { key: '15m', label: '15m', color: 'amber' },
    { key: '1h', label: '1h', color: 'emerald' },
    { key: '4h', label: '4h', color: 'rose' },
  ];

  // Common color rules
  const rsiRules: Array<{ op: string; value: number; class: string }> = [
    { op: 'gt', value: 70, class: 'text-red-400 dark:text-red-300' },
    { op: 'lt', value: 30, class: 'text-emerald-400 dark:text-emerald-300' },
  ];
  const stochRules = [
    { op: 'gt', value: 80, class: 'text-red-400 dark:text-red-300' },
    { op: 'lt', value: 20, class: 'text-emerald-400 dark:text-emerald-300' },
  ];
  const adxRules = [
    { op: 'gt', value: 25, class: 'text-amber-400 dark:text-amber-300' },
  ];
  const defaultClass = 'text-slate-300 dark:text-slate-400';

  // Slot factory
  function slot(
    key: string, label: string, format: IndicatorSlot['format'],
    decimals: number, bold: boolean, fontSize: string,
    colorRules: IndicatorSlot['color_rules'],
    chartMeta: ChartMeta,
  ): IndicatorSlot {
    return {
      key, label, format, decimals, bold,
      font_size: fontSize,
      color_rules: colorRules,
      default_class: defaultClass,
      chart_meta: chartMeta,
    };
  }

  const indicators: Record<string, IndicatorSlot[]> = {
    '1m': [
      slot('1m_rsi_14', 'RSI', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', rsiRules, {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [30, 50, 70], color: '#a78bfa',
      }),
      slot('1m_ema_9', 'EMA9', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#22d3ee', line_style: 2, line_width: 1,
      }),
      slot('1m_ema_21', 'EMA21', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#38bdf8', line_style: 2, line_width: 1,
      }),
    ],
    '15m': [
      slot('15m_rsi_14', 'RSI', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', rsiRules, {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [30, 50, 70], color: '#a78bfa',
      }),
      slot('15m_stoch_k_14_3_3', 'StoK', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', stochRules, {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [20, 50, 80], color: '#f472b6',
      }),
      slot('15m_stoch_d_14_3_3', 'StoD', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', stochRules, {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [20, 50, 80], color: '#fb7185',
      }),
      slot('15m_atr_14', 'ATR', 'decimal', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'area', series: 'area', y_range: null,
        ref_lines: [], color: '#fb923c',
      }),
      slot('15m_ema_9', 'EMA9', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#22d3ee', line_style: 2, line_width: 1,
      }),
      slot('15m_ema_21', 'EMA21', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#38bdf8', line_style: 2, line_width: 1,
      }),
      slot('15m_bbands_upper', 'BB-U', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#a855f7', line_style: 1, line_width: 1,
      }),
      slot('15m_bbands_middle', 'BB-M', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#a855f780', line_style: 1, line_width: 1,
      }),
      slot('15m_bbands_lower', 'BB-L', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#a855f7', line_style: 1, line_width: 1,
      }),
    ],
    '1h': [
      slot('1h_rsi_14', 'RSI', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', rsiRules, {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [30, 50, 70], color: '#a78bfa',
      }),
      slot('1h_stoch_k_14_3_3', 'StoK', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', stochRules, {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [20, 50, 80], color: '#f472b6',
      }),
      slot('1h_stoch_d_14_3_3', 'StoD', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', stochRules, {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [20, 50, 80], color: '#fb7185',
      }),
      slot('1h_adx_14', 'ADX', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', adxRules, {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [25], color: '#fbbf24',
      }),
      slot('1h_adx_dmp_14', '+DI', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [], color: '#4ade80',
      }),
      slot('1h_adx_dmn_14', '-DI', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [], color: '#f87171',
      }),
      slot('1h_atr_14', 'ATR', 'decimal', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'area', series: 'area', y_range: null,
        ref_lines: [], color: '#fb923c',
      }),
      slot('1h_macd_hist_12_26_9', 'MACD-H', 'signed', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'histogram', series: 'histogram', y_range: null,
        ref_lines: [0], color: '#60a5fa',
      }),
      slot('1h_macd_line_12_26_9', 'MACD', 'signed', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'histogram', series: 'line', y_range: null,
        ref_lines: [], color: '#60a5fa',
      }),
      slot('1h_macd_signal_12_26_9', 'MACD-S', 'signed', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'histogram', series: 'line', y_range: null,
        ref_lines: [], color: '#f97316',
      }),
      slot('1h_ema_9', 'EMA9', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#22d3ee', line_style: 2, line_width: 1,
      }),
      slot('1h_ema_21', 'EMA21', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#38bdf8', line_style: 2, line_width: 1,
      }),
      slot('1h_sma_20', 'SMA20', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#e879f9', line_style: 1, line_width: 1,
      }),
      slot('1h_sma_50', 'SMA50', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#c084fc', line_style: 1, line_width: 1,
      }),
      slot('1h_bbands_upper', 'BB-U', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#a855f7', line_style: 1, line_width: 1,
      }),
      slot('1h_bbands_middle', 'BB-M', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#a855f780', line_style: 1, line_width: 1,
      }),
      slot('1h_bbands_lower', 'BB-L', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#a855f7', line_style: 1, line_width: 1,
      }),
    ],
    '4h': [
      slot('4h_adx_14', 'ADX', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', adxRules, {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [25], color: '#fbbf24',
      }),
      slot('4h_adx_dmp_14', '+DI', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [], color: '#4ade80',
      }),
      slot('4h_adx_dmn_14', '-DI', 'decimal', 1, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'oscillator', series: 'line', y_range: [0, 100],
        ref_lines: [], color: '#f87171',
      }),
      slot('4h_macd_hist_12_26_9', 'MACD-H', 'signed', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'histogram', series: 'histogram', y_range: null,
        ref_lines: [0], color: '#60a5fa',
      }),
      slot('4h_macd_line_12_26_9', 'MACD', 'signed', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'histogram', series: 'line', y_range: null,
        ref_lines: [], color: '#60a5fa',
      }),
      slot('4h_macd_signal_12_26_9', 'MACD-S', 'signed', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'histogram', series: 'line', y_range: null,
        ref_lines: [], color: '#f97316',
      }),
      slot('4h_ema_9', 'EMA9', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#22d3ee', line_style: 2, line_width: 1,
      }),
      slot('4h_ema_21', 'EMA21', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#38bdf8', line_style: 2, line_width: 1,
      }),
      slot('4h_sma_20', 'SMA20', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#e879f9', line_style: 1, line_width: 1,
      }),
      slot('4h_sma_50', 'SMA50', 'price', 2, false, 'clamp(12px, 1.1vw, 14px)', [], {
        pane: 'price_overlay', series: 'line', y_range: null,
        ref_lines: [], color: '#c084fc', line_style: 1, line_width: 1,
      }),
    ],
  };

  return { timeframes, grid_columns: 4, indicators };
}

// ── Pre-generated mock data for a symbol ──

export interface MockAssetData {
  symbol: string;
  category: string;
  price: number;
  futuresPrice: number;
  change: number;
  decimals: number;
  decision: string;
  vars: {
    pcr: number; signal: number; avg_iv: number; total_gex: number;
    whale_buy: number; whale_sell: number; whale_net: number;
    max_pain: number; support: number; support_secondary: number;
    resistance: number; resistance_secondary: number; skew: number;
    delta: number; gamma: number; iv: number; oi: number;
    oi_concentration: number; nearest_expiry: number;
  };
  indicators: Record<string, number | null>;
  strategies: Array<{
    key: string; name: string; label: string; color: string;
    bull_score: number; bear_score: number;
    rules_matched: string[];
    display: { label: string; order: number };
    timeframe: string | null;
  }>;
  candles: Record<string, MockCandle[]>;
  indicatorHistory: Record<string, Array<{ time: number; value: number }>>;
}

export function generateMockAsset(symbol: string, basePrice: number): MockAssetData {
  const config = getMockIndicatorConfig();
  const rand = seededRandom(hashString(symbol) * 7 + 42);

  // Generate candles per timeframe
  const candles: Record<string, MockCandle[]> = {};
  const candleCounts: Record<string, number> = { '1m': 100, '15m': 100, '1h': 100, '4h': 100 };

  for (const tf of config.timeframes) {
    candles[tf.key] = generateCandles(symbol, candleCounts[tf.key] || 100, tf.key, basePrice);
  }

  // Generate indicator histories for all slots
  const indicatorHistory: Record<string, Array<{ time: number; value: number }>> = {};
  for (const tf of config.timeframes) {
    const slots = config.indicators[tf.key] || [];
    const tfCandles = candles[tf.key] || [];
    for (const s of slots) {
      indicatorHistory[s.key] = generateIndicatorHistory(s.key, tfCandles.length, tfCandles, s.chart_meta);
    }
  }

  // Current indicator values (last value from history)
  const indicators: Record<string, number | null> = {};
  for (const [key, history] of Object.entries(indicatorHistory)) {
    const last = history[history.length - 1];
    indicators[key] = last ? last.value : null;
  }

  // Mock vars
  const change = +((rand() - 0.4) * 6).toFixed(2);
  const price = +(basePrice * (1 + change / 100)).toFixed(2);
  const futuresPrice = +(price * (1 + (rand() - 0.5) * 0.002)).toFixed(2);

  return {
    symbol,
    category: 'Crypto',
    price,
    futuresPrice,
    change,
    decimals: symbol === 'BTCUSDT' ? 2 : (symbol === 'ETHUSDT' ? 2 : 3),
    decision: change > 2 ? 'STRONG BUY' : change > 0.5 ? 'BUY' : change > -0.5 ? 'NEUTRAL' : change > -2 ? 'SELL' : 'STRONG SELL',
    vars: {
      pcr: +(0.8 + rand() * 0.8).toFixed(2),
      signal: +(40 + rand() * 50).toFixed(0) as any,
      avg_iv: +(35 + rand() * 25).toFixed(1) as any,
      total_gex: +(rand() * 10 - 3) * 1_000_000,
      whale_buy: +(rand() * 8 + 2) * 1_000_000,
      whale_sell: +(rand() * 6 + 1) * 1_000_000,
      whale_net: +(rand() * 4 - 1) * 1_000_000,
      max_pain: Math.round(basePrice * (0.98 + rand() * 0.04)),
      support: Math.round(basePrice * 0.97),
      support_secondary: Math.round(basePrice * 0.95),
      resistance: Math.round(basePrice * 1.03),
      resistance_secondary: Math.round(basePrice * 1.05),
      skew: +(rand() * 0.06 - 0.03).toFixed(4) as any,
      delta: +(0.3 + rand() * 0.4).toFixed(2) as any,
      gamma: +(rand() * 0.0002).toFixed(6) as any,
      iv: +(40 + rand() * 20).toFixed(1) as any,
      oi: Math.round(rand() * 500_000 + 100_000),
      oi_concentration: +(rand() * 0.3 + 0.1).toFixed(3) as any,
      nearest_expiry: +(1 + rand() * 14).toFixed(1) as any,
    },
    indicators,
    strategies: [
      {
        key: 'momentum_squeeze',
        name: 'Momentum Squeeze',
        label: change > 0 ? 'BULL SQUEEZE' : 'BEAR PRESSURE',
        color: change > 0 ? 'emerald' : 'red',
        bull_score: change > 0 ? 72 : 28,
        bear_score: change > 0 ? 28 : 72,
        rules_matched: change > 0 ? ['RSI > 50', 'EMA Stack Bullish', 'MACD Cross Up'] : ['RSI < 50', 'EMA Stack Bearish', 'MACD Cross Down'],
        display: { label: 'Squeeze', order: 0 },
        timeframe: '1h',
      },
      {
        key: 'session_breakout',
        name: 'Session Breakout',
        label: 'IB RANGE',
        color: 'amber',
        bull_score: 50,
        bear_score: 50,
        rules_matched: ['US Session Active', 'IB Range Narrow'],
        display: { label: 'SBS', order: 1 },
        timeframe: null,
      },
    ],
    candles,
    indicatorHistory,
  };
}

// ── Generate mock assets ──

export function getMockAssets(): MockAssetData[] {
  return [
    generateMockAsset('BTCUSDT', 97000),
    generateMockAsset('ETHUSDT', 3400),
  ];
}

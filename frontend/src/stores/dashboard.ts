/**
 * WebSocket Service for MarketPulse Dashboard
 * ==============================================
 * Connects to Django Channels WebSocket at /ws/market/,
 * subscribes to intelligence + index streams, and provides
 * reactive data to Alpine.js components.
 *
 * Backend Protocol (Django Channels Consumer):
 * ─────────────────────────────────────────────
 *   Client → Server (subscribe):
 *     { action: "subscribe", underlying: "BTCUSDT", category: "intelligence" }
 *     { action: "subscribe", underlying: "BTCUSDT", category: "index" }
 *
 *   Client → Server (unsubscribe):
 *     { action: "unsubscribe", underlying: "BTCUSDT", category: "intelligence" }
 *
 *   Server → Client (intelligence):
 *     {
 *       category: "intelligence",
 *       underlying: "BTCUSDT",
 *       data: { asset, score, signal, raw_score, index_price, stale,
 *               data_timestamps, metrics, top_symbols, strike_analysis,
 *               oi_concentration, nearest_expiry_days, reasons }
 *     }
 *
 *   Server → Client (index price):
 *     { category: "index", underlying: "BTCUSDT", data: { s: "BTCUSDT", p: "99102.32", ... } }
 *
 *   Server → Client (heartbeat):
 *     { type: "ping", timestamp: 1234567890 }
 *
 * REST Endpoint:
 *   GET /api/market/active-underlyings → { active_underlyings: ["BTCUSDT", "ETHUSDT"] }
 */

// ── Configuration Constants ──

const WS_PATH = '/ws/market/';

function getWsBaseUrl(): string {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const host = location.host;
  const base = `${protocol}://${host}${WS_PATH}`;

  // Append JWT token if available (for optional WS authentication)
  const token = localStorage.getItem('mp_access_token');
  if (token) {
    return `${base}?token=${encodeURIComponent(token)}`;
  }
  return base;
}

const INITIAL_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;
const BACKOFF_MULTIPLIER = 1.5;
const HEARTBEAT_INTERVAL_MS = 15_000;
const STALE_THRESHOLD_MS = 90_000;
const FLASH_DURATION_MS = 600;

// How long to wait after WS connects before logging a warning for assets
// still on CONNECTING. We do NOT remove them — we keep WS subscriptions
// alive so they auto-recover when data arrives. The only correct removal
// signal is `insufficient_data` from the backend (genuinely no tradeable options).
const CONNECTING_GRACE_PERIOD_MS = 30_000;

const DEFAULT_UNDERLYINGS = ['BTCUSDT', 'ETHUSDT'] as const;

// ── WebSocket Message Types ──

interface WSSubscribePayload {
  readonly action: 'subscribe';
  readonly underlying: string;
  readonly category: string;
  readonly broker?: string;
}

interface WSUnsubscribePayload {
  readonly action: 'unsubscribe';
  readonly underlying: string;
  readonly category: string;
  readonly broker?: string;
}

type WSSendPayload = WSSubscribePayload | WSUnsubscribePayload;

interface WSIncomingMessage {
  type?: string;
  status?: 'subscribed' | 'unsubscribed';
  category?: string;
  underlying?: string;
  data?: any;
}

// ── Event Types ──

type ConnectionStatus = 'connected' | 'disconnected' | 'error';

interface ConnectionChangeEvent {
  status: ConnectionStatus;
}

type WSEventCallback = (data: any) => void;

// ── Decision Color Maps (static — created once) ──

const DOT_COLOR_MAP: Readonly<Record<string, string>> = {
  'STRONG BUY': 'bg-emerald-400',
  'BUY': 'bg-emerald-400/70',
  'NEUTRAL': 'bg-amber-400',
  'NATURAL': 'bg-amber-400',
  'SELL': 'bg-red-400/70',
  'STRONG SELL': 'bg-red-400',
  'CONNECTING': 'bg-sky-400',
};
const DOT_COLOR_DEFAULT = 'bg-gray-400';

const DECISION_SURFACE_MAP: Readonly<Record<string, string>> = {
  'STRONG BUY': 'bg-emerald-600 border-emerald-500 text-white',
  'BUY': 'bg-emerald-500/90 border-emerald-400 text-white',
  'NEUTRAL': 'bg-amber-500/90 border-amber-400 text-white',
  'NATURAL': 'bg-amber-500/90 border-amber-400 text-white',
  'SELL': 'bg-red-500/90 border-red-400 text-white',
  'STRONG SELL': 'bg-red-600 border-red-500 text-white',
  'CONNECTING': 'bg-slate-500/60 border-slate-400/40 text-slate-200',
};
const SURFACE_CLASS_DEFAULT = 'bg-gray-500/50 border-gray-400/30 text-gray-200';

// ── Variable Formatters (static — created once) ──

type VarFormatter = (v: number) => string;

const VAR_FORMATTERS: Readonly<Record<string, VarFormatter>> = {
  pcr: (v) => v.toFixed(2),
  avg_iv: (v) => v.toFixed(1) + '%',
  delta: (v) => v.toFixed(2),
  gamma: (v) => v.toFixed(4),
  skew: (v) => {
    const pct = v * 100;
    return (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
  },
  total_gex: (v) => (v / 1_000_000).toFixed(1) + 'M',
  signal: (v) => v.toFixed(0),
  whale_buy: (v) => '$' + (v / 1_000_000).toFixed(1) + 'M',
  whale_sell: (v) => '$' + (v / 1_000_000).toFixed(1) + 'M',
  whale_net: (v) => '$' + (v / 1_000_000).toFixed(1) + 'M',
  max_pain: (v) => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
  support: (v) => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
  support_secondary: (v) => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
  resistance: (v) => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
  resistance_secondary: (v) => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
  oi_concentration: (v) => (v * 100).toFixed(1) + '%',
  nearest_expiry: (v) => v.toFixed(1) + 'd',
  iv: (v) => v.toFixed(1) + '%',
  oi: (v) => v.toLocaleString('en-US', { maximumFractionDigits: 0 }),
};


// ═══════════════════════════════════════════════
//  Tooltip Data — Global
// ═══════════════════════════════════════════════

import tooltipData from '../data/tooltips.json';

/**
 * Read tooltip data from the JSON — used by the store's showTooltip/hideTooltip.
 * Kept as a plain import (not in Alpine scope) so it doesn't get affected by re-renders.
 */
(window as any)._tooltipData = tooltipData;


// ═══════════════════════════════════════════════
//  MarketWebSocket — Singleton WebSocket Service
// ═══════════════════════════════════════════════

class MarketWebSocket {
  private ws: WebSocket | null = null;
  private listeners = new Map<string, Set<WSEventCallback>>();
  private pendingSubscriptions = new Map<string, WSSendPayload>();
  private reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private lastMessageTime = 0;
  private intentionalClose = false;

  constructor() {
    this._onMessage = this._onMessage.bind(this);
  }

  /** Open WebSocket connection (idempotent) */
  connect() {
    this.intentionalClose = false;

    if (
      this.ws &&
      (this.ws.readyState === WebSocket.CONNECTING ||
        this.ws.readyState === WebSocket.OPEN)
    ) {
      return;
    }

    try {
      this.ws = new WebSocket(getWsBaseUrl());
    } catch (e) {
      console.error('[WS] Failed to create WebSocket:', e);
      this._scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log('[WS] Connected to', WS_PATH);
      this.reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
      this.lastMessageTime = Date.now();
      this._startHeartbeat();
      this._emit('connection_change', { status: 'connected' });
      // Re-subscribe to all groups using saved payloads
      this.pendingSubscriptions.forEach((payload) => {
        this._rawSend(payload);
      });
    };

    this.ws.onmessage = this._onMessage;

    this.ws.onclose = (event) => {
      console.warn(`[WS] Closed (code=${event.code}, reason="${event.reason || 'none'}")`);
      this._stopHeartbeat();
      this._emit('connection_change', { status: 'disconnected' });
      // Only auto-reconnect if not intentionally closed and not a normal closure
      if (!this.intentionalClose && event.code !== 1000) {
        this._scheduleReconnect();
      }
    };

    this.ws.onerror = (error) => {
      console.error('[WS] Error:', error);
      this._emit('connection_change', { status: 'error' });
    };
  }

  /** Clean disconnect — prevents automatic reconnection */
  disconnect() {
    this.intentionalClose = true;
    this._stopHeartbeat();
    this._clearReconnectTimer();
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.onerror = null;
      this.ws.close(1000, 'Client disconnect');
      this.ws = null;
    }
  }

  /**
   * Subscribe to a data stream.
   */
  subscribe(underlying: string, category: string, broker: string = 'binance') {
    const key = `${broker}:${underlying}:${category}`;
    if (this.pendingSubscriptions.has(key)) return;

    const payload: WSSubscribePayload = { action: 'subscribe', underlying, category, broker };
    this.pendingSubscriptions.set(key, payload);

    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this._rawSend(payload);
    }
  }

  /**
   * Unsubscribe from a data stream.
   */
  unsubscribe(underlying: string, category: string, broker: string = 'binance') {
    const key = `${broker}:${underlying}:${category}`;
    this.pendingSubscriptions.delete(key);

    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this._rawSend({ action: 'unsubscribe', underlying, category, broker });
    }
  }

  /** Register an event listener. Returns unsubscribe function. */
  on(event: string, callback: WSEventCallback): () => void {
    if (!this.listeners.has(event)) {
      this.listeners.set(event, new Set());
    }
    this.listeners.get(event)!.add(callback);
    return () => { this.listeners.get(event)?.delete(callback); };
  }

  /** Send raw JSON to WebSocket (only if open) */
  private _rawSend(data: WSSendPayload) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  /** Route incoming WebSocket messages to registered listeners */
  private _onMessage(event: MessageEvent) {
    let payload: WSIncomingMessage;
    try {
      payload = JSON.parse(event.data as string);
    } catch {
      return;
    }

    this.lastMessageTime = Date.now();

    // Heartbeat ping from server
    if (payload.type === 'ping') return;

    // Subscription confirmation
    if (payload.status === 'subscribed' || payload.status === 'unsubscribed') {
      this._emit('subscription', payload);
      return;
    }

    // Error notification
    if (payload.category === 'error') {
      this._emit('error', payload);
      return;
    }

    // Market data events — route by category + underlying
    if (payload.category && payload.underlying) {
      this._emit(`data:${payload.category}`, payload);
      this._emit('data', payload);
    }
  }

  /** Emit event to all registered listeners for that event type */
  private _emit(event: string, data: any) {
    const callbacks = this.listeners.get(event);
    if (callbacks) {
      callbacks.forEach((cb) => {
        try { cb(data); } catch (e) { console.error(`[WS] Listener error on "${event}":`, e); }
      });
    }
  }

  /** Schedule reconnect with exponential backoff */
  private _scheduleReconnect() {
    this._clearReconnectTimer();
    const delay = this.reconnectDelay;
    console.log(`[WS] Scheduling reconnect in ${delay}ms...`);
    this.reconnectTimer = setTimeout(() => {
      this.connect();
      this.reconnectDelay = Math.min(
        this.reconnectDelay * BACKOFF_MULTIPLIER,
        MAX_RECONNECT_DELAY_MS,
      );
    }, delay);
  }

  /** Start heartbeat monitor — checks for stale connection */
  private _startHeartbeat() {
    this._stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        const elapsed = Date.now() - this.lastMessageTime;
        if (elapsed > STALE_THRESHOLD_MS) {
          console.warn(
            `[WS] No message received in ${STALE_THRESHOLD_MS / 1000}s — connection is stale. Closing.`,
          );
          this.ws.close(4000, 'Stale connection');
        }
      }
    }, HEARTBEAT_INTERVAL_MS);
  }

  private _stopHeartbeat() {
    if (this.heartbeatTimer !== null) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private _clearReconnectTimer() {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }
}

// ── Singleton instance ──
export const marketWS = new MarketWebSocket();


/**
 * Alpine.js Dashboard Store
 * ==========================
 * Reactive state management for the options intelligence dashboard.
 * Bridges WebSocket data into Alpine.js reactive properties.
 */

// ── Public Interfaces ──

export interface AssetVars {
  pcr: number | null;
  signal: number | null;
  avg_iv: number | null;
  total_gex: number | null;
  whale_buy: number | null;
  whale_sell: number | null;
  whale_net: number | null;
  max_pain: number | null;
  support: number | null;
  support_secondary: number | null;
  resistance: number | null;
  resistance_secondary: number | null;
  skew: number | null;
  delta: number | null;
  gamma: number | null;
  iv: number | null;
  oi: number | null;
  oi_concentration: number | null;
  nearest_expiry: number | null;
}

/**
 * Technical indicators from futures kline pipeline.
 * Key format: "{timeframe}_{indicator}_{param}" e.g. "1m_rsi_14", "1h_ema_9"
 */
export interface Indicators {
  [key: string]: number | null;
}

export interface AssetState {
  symbol: string;
  category: string;
  price: number;
  basePrice: number;
  change: number;
  decimals: number;
  flash: 'up' | 'down' | null;
  decision: string;
  score: number;
  rawScore: number;
  reasons: string[];
  stale: string[];
  dataTimestamps: Record<string, string>;
  vars: AssetVars;
  /** Technical indicators from futures kline pipeline (separate from intelligence) */
  indicators: Indicators;
  /** Strategy decisions from backend compute_strategies() — array of 2-3 strategies */
  strategies: Array<{
    key: string;
    name: string;
    label: string;
    color: string;
    bull_score: number;
    bear_score: number;
    rules_matched: string[];
    display: { label: string; order: number };
    timeframe: string | null;
  }> | null;
  strikeAnalysis: Array<{
    strike: number;
    distance_pct: number;
    call_oi: number;
    put_oi: number;
    net_oi: number;
    call_iv: number;
    put_iv: number;
    net_gex: number;
  }>;
}

export interface DashboardStore {
  ws: MarketWebSocket | null;
  connectionStatus: 'connecting' | 'connected' | 'disconnected' | 'error';
  selectedAsset: string | null;
  assets: Record<string, AssetState>;
  activeUnderlyings: string[];
  darkMode: boolean;
  activeNav: string;
  /** Broker-centric: currently selected broker (default: 'binance') */
  activeBroker: string;
  /** List of available brokers from /api/brokers/list */
  availableBrokers: Array<{ id: string; display_name: string }>;
  /** Switch broker — unsubscribes old, subscribes new */
  switchBroker(broker: string): void;
  /** Indicator display config fetched from backend — drives dynamic rendering */
  indicatorConfig: any;
  init(): Promise<void>;
  destroy(): void;
  selectAsset(symbol: string | null): void;
  assetList: AssetState[];
  filteredAssets: AssetState[];
  fmtPrice(p: number, d: number): string;
  fmtVar(key: string, v: number | null): string;
  /** Format indicator value using backend-provided slot config */
  formatSlot(slot: any, asset: AssetState): string;
  /** Get CSS class for an indicator slot based on value + color rules */
  slotClass(slot: any, asset: AssetState): string;
  /** Get strategy item color class from color string */
  strategyItemColorClass(color: string): string;
  /** Check if overall context is bearish (for TP/SL direction flip) */
  isBearish(asset: AssetState): boolean;
  /** Extract SBS (Session Breakout Strategy) enriched data */
  getSbsData(asset: AssetState): any | null;
  dotColor(d: string): string;
  decisionSurfaceClass(d: string): string;
  connectionLabel: string;
  connectionColor: string;
  varLabels: Array<{ key: string; label: string }>;
  navItems: Array<{ id: string; label: string; icon: string; badge?: string }>;
  sysItems: Array<{ id: string; label: string; icon: string; badge?: string }>;
  /** Market sessions data from backend — published every 30s */
  marketSessions: {
    sessions: Record<string, {
      active: boolean;
      label: string;
      color: string;
      weight: number;
    }>;
    active: string[];
    label: string;
    color: string;
    utc_hour: number;
    weight: number;
  } | null;
  /** Computed: primary session label for topbar display */
  sessionLabel: string;
  /** Computed: primary session color for topbar display */
  sessionColor: string;
  /** Computed: background pill color */
  sessionBg: string;
  /** Computed: dot color */
  sessionDotColor: string;
  /** Tooltip state — survives Alpine re-renders */
  tip: {
    visible: boolean;
    title: string;
    content: string;
    /** Full tooltip data object for structured rendering */
    data: any;
    /** Positioning — set by showTooltip from the DOM element */
    posX: number;
    posY: number;
    /** 'above' or 'below' — auto-flipped when near viewport edge */
    dir: string;
  };
  /** Show tooltip — call from @mouseenter on any tip-trigger element */
  showTooltip(group: string, key: string, fallbackGroup?: string, fallbackKey?: string): void;
  /** Hide tooltip — call from @mouseleave */
  hideTooltip(): void;
}

// ── Internal Types (not exposed in public API) ──

/** @internal Extends AssetState with private runtime fields */
interface InternalAssetState extends AssetState {
  _flashTimer: ReturnType<typeof setTimeout> | null;
}

/** @internal Navigation item with optional badge */
interface NavItem {
  readonly id: string;
  readonly label: string;
  readonly icon: string;
  readonly badge?: string;
}

/** @internal Extends DashboardStore with private lifecycle methods */
interface InternalDashboardStore extends DashboardStore {
  _fetchActiveUnderlyings(): Promise<void>;
  _fetchIndicatorConfig(): Promise<void>;
  _setupWebSocket(): void;
  _updateAssetFromIntelligence(underlying: string, intel: any): void;
  _removeAsset(underlying: string, reason?: string): void;
  _emptyAsset(symbol: string): AssetState;
  _updateAssetFromIndicators(underlying: string, data: any): void;
  _startConnectingTimeout(): void;
  _stopConnectingTimeout(): void;
  strategyItemColorClass(color: string): string;
  isBearish(asset: AssetState): boolean;
  sessionBg: string;
  sessionDotColor: string;
}


// ═══════════════════════════════════════════════
//  Store Factory
// ═══════════════════════════════════════════════

export function createDashboardStore(): DashboardStore {
  // Track WS unsubscribe functions for cleanup
  let wsUnsubscribers: Array<() => void> = [];
  // Timer to remove assets stuck on CONNECTING after WS is live
  let connectingTimer: ReturnType<typeof setTimeout> | null = null;

  // ── Navigation items (SVG icons inlined) ──
  const navItems: NavItem[] = [
    {
      id: 'dashboard',
      label: 'Dashboard',
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-[18px] h-[18px]"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    },
    {
      id: 'markets',
      label: 'Markets',
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-[18px] h-[18px]"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
    },
    {
      id: 'watchlist',
      label: 'Watchlist',
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-[18px] h-[18px]"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
    },
    {
      id: 'portfolio',
      label: 'Portfolio',
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-[18px] h-[18px]"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>',
    },
    {
      id: 'orders',
      label: 'Orders',
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-[18px] h-[18px]"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
      badge: '5',
    },
  ];

  const sysItems: NavItem[] = [
    {
      id: 'alerts',
      label: 'Alerts',
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-[18px] h-[18px]"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
      badge: '3',
    },
    {
      id: 'settings',
      label: 'Settings',
      icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="w-[18px] h-[18px]"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.26.604.852.997 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    },
  ];

  const store: InternalDashboardStore = {
    ws: null,
    connectionStatus: 'connecting',
    selectedAsset: null,
    assets: {},
    activeUnderlyings: [],
    darkMode: true,
    activeNav: 'dashboard',
    activeBroker: 'binance',  // Broker-centric: default to Binance
    availableBrokers: [{ id: 'binance', display_name: 'Binance' }],  // Default; updated by /api/brokers/list
    marketSessions: null,
    indicatorConfig: null,

    // ── Global Tooltip State (survives re-renders) ──
    tip: { visible: false, title: '', content: '', data: null, posX: 0, posY: 0, dir: 'above' },
    _tipHideTimer: null as ReturnType<typeof setTimeout> | null,

    // ── Variable labels for the 7-column display ──
    varLabels: [
      { key: 'pcr', label: 'PCR' },
      { key: 'avg_iv', label: 'IV Rank' },
      { key: 'delta', label: 'Delta' },
      { key: 'gamma', label: 'Gamma' },
      { key: 'skew', label: 'Skew' },
      { key: 'total_gex', label: 'GEX' },
      { key: 'signal', label: 'Signal' },
    ],

    navItems,
    sysItems,

    // ── Timeframe color lookups (for indicator grid) ──
    tfColorClass: {
      sky: 'text-sky-500 dark:text-sky-400',
      amber: 'text-amber-500 dark:text-amber-400',
      violet: 'text-violet-500 dark:text-violet-400',
      rose: 'text-rose-500 dark:text-rose-400',
    },
    tfBgClass: {
      sky: 'hover:bg-sky-500/5',
      amber: 'hover:bg-amber-500/5',
      violet: 'hover:bg-violet-500/5',
      rose: 'hover:bg-rose-500/5',
    },

    // ═══════════════════════════════════════
    //  LIFECYCLE
    // ═══════════════════════════════════════

    async init() {
      await this._fetchAvailableBrokers();
      await this._fetchActiveUnderlyings();
      await this._fetchIndicatorConfig();
      this._setupWebSocket();
    },

    async _fetchAvailableBrokers() {
      /**
       * Fetch list of available brokers for the sidebar selector.
       * Falls back to a hardcoded Binance entry if the API fails.
       */
      try {
        const resp = await fetch('/api/brokers/list');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        this.availableBrokers = data.brokers || [];
      } catch (e) {
        console.warn('[Dashboard] Failed to fetch brokers, using fallback:', e);
        this.availableBrokers = [{ id: 'binance', display_name: 'Binance' }];
      }
    },

    async _fetchActiveUnderlyings() {
      try {
        const resp = await fetch(`/api/market/active-underlyings?broker=${this.activeBroker}`);
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
        }
        const data = await resp.json();
        this.activeUnderlyings = data.active_underlyings || [...DEFAULT_UNDERLYINGS];
        this.activeUnderlyings.forEach((symbol: string) => {
          if (!this.assets[symbol]) {
            this.assets[symbol] = this._emptyAsset(symbol);
          }
        });
      } catch (e) {
        console.error('[Dashboard] Failed to fetch active underlyings:', e);
        this.activeUnderlyings = [...DEFAULT_UNDERLYINGS];
        this.activeUnderlyings.forEach((s: string) => {
          if (!this.assets[s]) this.assets[s] = this._emptyAsset(s);
        });
      }
    },

    async _fetchIndicatorConfig() {
      /**
       * Fetch indicator display config from backend.
       * This drives the dynamic rendering of all indicator rows.
       * Fetched once on init — adding a new indicator to the backend
       * registry automatically updates the frontend on next page load.
       */
      try {
        const resp = await fetch('/api/market/indicator-config');
        if (!resp.ok) return;
        this.indicatorConfig = await resp.json();
      } catch (e) {
        console.warn('[Dashboard] Failed to fetch indicator config:', e);
      }
    },

    _setupWebSocket() {
      const ws = marketWS;
      this.ws = ws;
      this.connectionStatus = 'connecting';

      wsUnsubscribers.push(
        ws.on('connection_change', ({ status }: ConnectionChangeEvent) => {
          this.connectionStatus = status as DashboardStore['connectionStatus'];
          // Start grace period timer once connected — clean up stale CONNECTING assets
          if (status === 'connected') {
            this._startConnectingTimeout();
          } else {
            this._stopConnectingTimeout();
          }
        }),
      );

      this.activeUnderlyings.forEach((symbol: string) => {
        ws.subscribe(symbol, 'intelligence', this.activeBroker);
        ws.subscribe(symbol, 'index', this.activeBroker);
        ws.subscribe(symbol, 'indicators', this.activeBroker);
      });

      // ── Intelligence data handler ──
      // F2 FIX: guard against stale data from wrong broker arriving
      // after a broker switch (race condition on late channel messages).
      wsUnsubscribers.push(
        ws.on('data:intelligence', (payload: any) => {
          if (payload.broker && payload.broker !== this.activeBroker) return;
          const underlying = payload.underlying;
          const intel = payload.data;

          this._updateAssetFromIntelligence(underlying, intel);
        }),
      );

      // ── Index price handler ──
      wsUnsubscribers.push(
        ws.on('data:index', (payload: any) => {
          if (payload.broker && payload.broker !== this.activeBroker) return;
          const data = payload.data;
          if (!data) return;

          const symbol = data.s || payload.underlying;
          const price = parseFloat(data.p || '0');
          if (!price) return;

          const asset = this.assets[symbol];
          if (asset) {
            const oldPrice = asset.price || 0;
            asset.price = price;

            if (oldPrice > 0 && asset.basePrice > 0) {
              const pctChange = ((price - asset.basePrice) / asset.basePrice) * 100;
              asset.change = parseFloat(pctChange.toFixed(2));

              if (price > oldPrice) {
                asset.flash = 'up';
              } else if (price < oldPrice) {
                asset.flash = 'down';
              }

              const internalAsset = asset as InternalAssetState;
              if (internalAsset._flashTimer !== null) {
                clearTimeout(internalAsset._flashTimer);
              }
              internalAsset._flashTimer = setTimeout(() => {
                if (this.assets[symbol]) {
                  this.assets[symbol].flash = null;
                }
              }, FLASH_DURATION_MS);
            }
          }
          // Auto-recovery: if asset was removed but index price arrives, re-create
          if (!asset && this.activeUnderlyings.includes(symbol)) {
            this.assets[symbol] = this._emptyAsset(symbol);
            this.assets[symbol].price = price;
            this.assets[symbol].basePrice = price;
          }
        }),
      );

      // ── Indicators handler (futures kline pipeline — independent) ──
      wsUnsubscribers.push(
        ws.on('data:indicators', (payload: any) => {
          if (payload.broker && payload.broker !== this.activeBroker) return;
          const underlying = payload.underlying;
          const data = payload.data;
          if (!data) return;
          this._updateAssetFromIndicators(underlying, data);
        }),
      );

      // ── Error handler ──
      wsUnsubscribers.push(
        ws.on('error', (payload: any) => {
          console.warn('[Dashboard] Backend error:', payload.data?.message);
        }),
      );

      // ── Market sessions handler (global — no per-symbol subscription) ──
      wsUnsubscribers.push(
        ws.on('data:sessions', (payload: any) => {
          const data = payload.data;
          if (!data) return;
          this.marketSessions = data;
        }),
      );

      ws.connect();
    },

    // ═══════════════════════════════════════
    //  CONNECTING TIMEOUT
    // ═══════════════════════════════════════

    _startConnectingTimeout() {
      this._stopConnectingTimeout();
      connectingTimer = setTimeout(() => {
        const staleAssets = Object.entries(this.assets)
          .filter(([, a]) => (a as AssetState).decision === 'CONNECTING')
          .map(([symbol]) => symbol);

        if (staleAssets.length > 0) {
          console.log(
            `[Dashboard] ${staleAssets.length} asset(s) still on CONNECTING after ${CONNECTING_GRACE_PERIOD_MS / 1000}s — keeping (will auto-recover when data arrives):`,
            staleAssets,
          );
          // Do NOT remove assets. Keep WS subscriptions alive so they
          // auto-recover when the backend sends intelligence/indicator data.
          // Only `insufficient_data` from backend should trigger removal.
        }

        connectingTimer = null;
      }, CONNECTING_GRACE_PERIOD_MS);
    },

    _stopConnectingTimeout() {
      if (connectingTimer !== null) {
        clearTimeout(connectingTimer);
        connectingTimer = null;
      }
    },

    // ═══════════════════════════════════════
    //  DATA MAPPING
    // ═══════════════════════════════════════

    _updateAssetFromIntelligence(underlying: string, intel: any) {
      // Auto-recovery: if asset was removed but data arrives, re-create it
      // and re-subscribe to WS channels.
      if (!this.assets[underlying]) {
        this.assets[underlying] = this._emptyAsset(underlying);
        // Re-add to activeUnderlyings if it was removed
        if (!this.activeUnderlyings.includes(underlying)) {
          this.activeUnderlyings.push(underlying);
        }
        // Re-subscribe to WS channels if connection is live
        if (this.ws && this.connectionStatus === 'connected') {
          this.ws.subscribe(underlying, 'intelligence', this.activeBroker);
          this.ws.subscribe(underlying, 'index', this.activeBroker);
          this.ws.subscribe(underlying, 'indicators', this.activeBroker);
        }
      }

      const asset = this.assets[underlying];

      // Handle insufficient data — remove asset from active list.
      // This asset has no tradeable options on Binance, so it should not
      // appear in the dashboard at all.
      if (intel.status === 'insufficient_data') {
        this._removeAsset(underlying, 'insufficient_data from backend');
        return;
      }

      if (!asset.basePrice && intel.index_price) {
        asset.basePrice = intel.index_price;
      }

      asset.price = intel.index_price || asset.price;
      asset.decision = intel.signal || 'CONNECTING';
      asset.score = intel.score || 0;
      asset.rawScore = intel.raw_score || 0;
      asset.reasons = intel.reasons || [];
      asset.stale = intel.stale || [];

      const m = intel.metrics || {};
      asset.vars.pcr = m.pcr ?? null;
      asset.vars.signal = intel.score ?? null;
      asset.vars.avg_iv = m.avg_iv ?? null;
      asset.vars.total_gex = m.total_gex ?? null;
      asset.vars.whale_buy = m.whale_buy_volume ?? null;
      asset.vars.whale_sell = m.whale_sell_volume ?? null;
      const buyVol = m.whale_buy_volume ?? 0;
      const sellVol = m.whale_sell_volume ?? 0;
      asset.vars.whale_net =
        buyVol !== null && sellVol !== null ? buyVol - sellVol : null;
      asset.vars.max_pain = m.max_pain ?? null;
      asset.vars.support = m.support ?? null;
      asset.vars.support_secondary = m.support_secondary ?? null;
      asset.vars.resistance = m.resistance ?? null;
      asset.vars.resistance_secondary = m.resistance_secondary ?? null;
      asset.vars.skew = m.skew ?? null;
      asset.vars.oi_concentration = intel.oi_concentration ?? null;
      asset.vars.nearest_expiry = intel.nearest_expiry_days ?? null;

      if (intel.top_symbols && intel.top_symbols.length > 0) {
        const atm = intel.top_symbols[0];
        asset.vars.delta = atm.delta ?? null;
        asset.vars.gamma = atm.gamma ?? null;
        asset.vars.iv = atm.iv ?? null;
        asset.vars.oi = atm.oi ?? null;
      }

      asset.strikeAnalysis = intel.strike_analysis || [];
      asset.dataTimestamps = intel.data_timestamps || {};
    },

    _updateAssetFromIndicators(underlying: string, data: any): void {
      // Auto-recovery: if asset was removed but indicator data arrives, re-create it
      // and re-subscribe to WS channels.
      if (!this.assets[underlying]) {
        this.assets[underlying] = this._emptyAsset(underlying);
        if (!this.activeUnderlyings.includes(underlying)) {
          this.activeUnderlyings.push(underlying);
        }
        if (this.ws && this.connectionStatus === 'connected') {
          this.ws.subscribe(underlying, 'intelligence', this.activeBroker);
          this.ws.subscribe(underlying, 'index', this.activeBroker);
          this.ws.subscribe(underlying, 'indicators', this.activeBroker);
        }
      }
      const asset = this.assets[underlying];
      // data is now { values: {...}, strategy: {...} }
      if (data.values) {
        asset.indicators = { ...data.values };
      } else {
        // Backward compat: if backend sends flat dict (old format)
        asset.indicators = { ...data };
      }
      if (data.strategies) {
        asset.strategies = data.strategies;
      } else if (data.strategy) {
        // Backward compat: single strategy → wrap in array
        asset.strategies = [{
          key: 'legacy', name: data.strategy.label, label: data.strategy.label,
          color: data.strategy.color, bull_score: data.strategy.bull_score || 0,
          bear_score: data.strategy.bear_score || 0, rules_matched: data.strategy.rules_matched || [],
          display: { label: 'Strat', order: 0 }, timeframe: null,
        }];
      }
    },

    _removeAsset(underlying: string, reason?: string) {
      // Clean up flash timer if it exists
      const internal = this.assets[underlying] as InternalAssetState | undefined;
      if (internal && internal._flashTimer !== null) {
        clearTimeout(internal._flashTimer);
      }

      // Remove from assets map
      delete this.assets[underlying];

      // Remove from active underlyings list
      const idx = this.activeUnderlyings.indexOf(underlying);
      if (idx !== -1) {
        this.activeUnderlyings.splice(idx, 1);
      }

      // Unsubscribe from WS channels for this asset
      const ws = this.ws;
      if (ws) {
        ws.unsubscribe(underlying, 'intelligence', this.activeBroker);
        ws.unsubscribe(underlying, 'index', this.activeBroker);
        ws.unsubscribe(underlying, 'indicators', this.activeBroker);
      }

      // Clear selection if this was the selected asset
      if (this.selectedAsset === underlying) {
        this.selectedAsset = null;
      }

      console.log(`[Dashboard] Removed "${underlying}" — ${reason || 'insufficient options data'}`);
    },

    _emptyAsset(symbol: string): AssetState {
      const state: InternalAssetState = {
        symbol,
        category: 'Crypto',
        price: 0,
        basePrice: 0,
        change: 0,
        decimals: 2,
        flash: null,
        _flashTimer: null,
        decision: 'CONNECTING',
        score: 0,
        rawScore: 0,
        reasons: [],
        stale: [],
        dataTimestamps: {},
        vars: {
          pcr: null, signal: null, avg_iv: null, total_gex: null,
          whale_buy: null, whale_sell: null, whale_net: null,
          max_pain: null, support: null, support_secondary: null,
          resistance: null, resistance_secondary: null, skew: null,
          delta: null, gamma: null, iv: null, oi: null,
          oi_concentration: null, nearest_expiry: null,
        },
        strikeAnalysis: [],
        indicators: {},
        strategies: null,
      };
      return state;
    },

    // ═══════════════════════════════════════
    //  COMPUTED PROPERTIES
    // ═══════════════════════════════════════

    get assetList(): AssetState[] {
      // Stable sort by activeUnderlyings order (from backend ranking by strike count).
      // After auto-recovery, assets can jump to end of Record — this prevents drift.
      return this.activeUnderlyings
        .filter((sym: string) => this.assets[sym])
        .map((sym: string) => this.assets[sym]);
    },

    get filteredAssets(): AssetState[] {
      if (!this.selectedAsset) return this.assetList;
      return this.assetList.filter((a: AssetState) => a.symbol === this.selectedAsset);
    },

    get connectionLabel(): string {
      if (this.connectionStatus === 'connected') return 'WS LIVE';
      if (this.connectionStatus === 'connecting') return 'CONNECTING';
      return 'OFFLINE';
    },

    get connectionColor(): string {
      if (this.connectionStatus === 'connected') return 'text-emerald-400';
      if (this.connectionStatus === 'connecting') return 'text-sky-400';
      return 'text-red-400';
    },

    // ── Market Sessions (from backend — updated every 30s) ──

    get sessionLabel(): string {
      return this.marketSessions?.label || '';
    },

    get sessionColor(): string {
      if (!this.marketSessions?.label) return 'text-gray-500';
      const colorMap: Record<string, string> = {
        'violet': 'text-violet-400',
        'sky': 'text-sky-400',
        'amber': 'text-amber-400',
        'emerald': 'text-emerald-400',
        'rose': 'text-rose-400',
      };
      return colorMap[this.marketSessions.color] || 'text-gray-400';
    },

    get sessionBg(): string {
      if (!this.marketSessions?.label) return 'bg-gray-500/10';
      const bgMap: Record<string, string> = {
        'violet': 'bg-violet-500/10',
        'sky': 'bg-sky-500/10',
        'amber': 'bg-amber-500/10',
        'emerald': 'bg-emerald-500/10',
        'rose': 'bg-rose-500/10',
      };
      return bgMap[this.marketSessions.color] || 'bg-gray-500/10';
    },

    get sessionDotColor(): string {
      if (!this.marketSessions?.label) return 'bg-gray-500';
      const dotMap: Record<string, string> = {
        'violet': 'bg-violet-400',
        'sky': 'bg-sky-400',
        'amber': 'bg-amber-400',
        'emerald': 'bg-emerald-400',
        'rose': 'bg-rose-400',
      };
      return dotMap[this.marketSessions.color] || 'bg-gray-400';
    },

    // ═══════════════════════════════════════
    //  ACTIONS
    // ═══════════════════════════════════════

    selectAsset(symbol: string | null) {
      this.selectedAsset = symbol;
    },

    /**
     * Switch active broker — unsubscribes all WS channels,
     * re-fetches underlyings for new broker, re-subscribes.
     */
    switchBroker(broker: string) {
      if (broker === this.activeBroker) return;
      const oldBroker = this.activeBroker;
      this.activeBroker = broker;

      // Unsubscribe from all old broker channels
      const ws = this.ws;
      if (ws && ws.readyState === WebSocket.OPEN) {
        this.activeUnderlyings.forEach((symbol: string) => {
          ws.unsubscribe(symbol, 'intelligence', oldBroker);
          ws.unsubscribe(symbol, 'index', oldBroker);
          ws.unsubscribe(symbol, 'indicators', oldBroker);
        });
      }

      // Clear current assets
      this.assets = {};
      this.activeUnderlyings = [];

      // Re-fetch and re-subscribe for new broker
      this._fetchActiveUnderlyings().then(() => {
        this.activeUnderlyings.forEach((symbol: string) => {
          this.assets[symbol] = this._emptyAsset(symbol);
        });
        if (ws && ws.readyState === WebSocket.OPEN) {
          this.activeUnderlyings.forEach((symbol: string) => {
            ws.subscribe(symbol, 'intelligence', broker);
            ws.subscribe(symbol, 'index', broker);
            ws.subscribe(symbol, 'indicators', broker);
          });
        }
      });
    },

    // ═══════════════════════════════════════
    //  FORMATTERS
    // ═══════════════════════════════════════

    fmtPrice(p: number, d: number): string {
      return p.toLocaleString('en-US', {
        minimumFractionDigits: d,
        maximumFractionDigits: d,
      });
    },

    fmtVar(key: string, v: number | null): string {
      if (v === null || v === undefined) return '—';
      const formatter = VAR_FORMATTERS[key];
      return formatter ? formatter(v) : String(v);
    },

    // ═══════════════════════════════════════
    //  STRATEGY (from backend — no client-side calculation)
    // ═══════════════════════════════════════

    strategyItemColorClass(color: string): string {
      const colorMap: Record<string, string> = {
        'emerald': 'text-emerald-200',
        'red': 'text-red-200',
        'amber': 'text-amber-200',
        'gray': 'text-white/70',
      };
      return colorMap[color] || 'text-white/50';
    },

    isBearish(asset: AssetState): boolean {
      // TP/SL direction follows the OPTIONS signal only.
      // Strategies are supplementary context — they should NOT override
      // the primary direction decided by the options intelligence engine.
      // A STRONG BUY with one LEAN BEAR strategy should still show
      // TP above price / SL below price, not flipped.
      return ['SELL', 'STRONG SELL'].includes(asset.decision);
    },

    /**
     * Extract SBS (Session Breakout Strategy) data from asset strategies.
     * Returns the sbs_data object if the session_breakout strategy is present,
     * or null if not available.
     */
    getSbsData(asset: AssetState): any | null {
      if (!asset.strategies) return null;
      const sbs = asset.strategies.find((s: any) => s.key === 'session_breakout');
      if (!sbs || !sbs.sbs_data) return null;
      const d = sbs.sbs_data;
      // Only show if there's meaningful session data
      if (d.phase === 'no_session' && !d.session_label) return null;
      return {
        session: d.session,
        session_label: d.session_label || '',
        phase: d.phase || 'no_session',
        ib_high: d.ib_high,
        ib_low: d.ib_low,
        ib_range: d.ib_range,
        progressive_mid: d.progressive_mid,
        cme_gap: d.cme_gap,
        breakout_direction: d.breakout_direction,
        tp1: d.tp1,
        tp2: d.tp2,
        sl: d.sl,
        minutes_into_session: d.minutes_into_session || 0,
      };
    },

    // ═══════════════════════════════════════
    //  DYNAMIC INDICATOR RENDERING HELPERS
    // ═══════════════════════════════════════

    /**
     * Format an indicator value using its slot config from the backend.
     * Handles all format types: decimal, price, signed, volume.
     */
    formatSlot(slot: any, asset: AssetState): string {
      const val = asset.indicators[slot.key];
      if (val === null || val === undefined || isNaN(val)) return '—';
      const fmt: string = slot.format || 'decimal';
      const dec: number = slot.decimals ?? 1;

      if (fmt === 'price') {
        return '$' + val.toLocaleString('en-US', { maximumFractionDigits: val > 1000 ? 0 : dec });
      }
      if (fmt === 'signed') {
        return (val >= 0 ? '+' : '') + val.toFixed(dec);
      }
      if (fmt === 'volume') {
        if (val >= 1_000_000) return (val / 1_000_000).toFixed(1) + 'M';
        if (val >= 1_000) return (val / 1_000).toFixed(1) + 'K';
        return val.toFixed(0);
      }
      // decimal (default)
      return val.toFixed(dec);
    },

    /**
     * Get the CSS class for an indicator slot based on its value and color_rules.
     * First matching rule wins, otherwise returns default_class.
     */
    slotClass(slot: any, asset: AssetState): string {
      const val = asset.indicators[slot.key];
      if (val === null || val === undefined || isNaN(val)) return slot.default_class || 'text-gray-300 dark:text-gray-400';

      const rules: Array<{op: string; value: number; class: string}> = slot.color_rules || [];
      for (const rule of rules) {
        if (rule.op === 'gt' && val > rule.value) return rule.class;
        if (rule.op === 'lt' && val < rule.value) return rule.class;
        if (rule.op === 'gte' && val >= rule.value) return rule.class;
        if (rule.op === 'lte' && val <= rule.value) return rule.class;
      }
      return slot.default_class || 'text-gray-300 dark:text-gray-400';
    },

    // ═══════════════════════════════════════
    //  TOOLTIP — Global (survives re-renders)
    // ═══════════════════════════════════════

    /**
     * Show a tooltip for the given group/key.
     * Falls back to fallbackGroup/fallbackKey if the primary entry is missing.
     * Uses a delayed hide timer so rapid re-renders don't kill the tooltip.
     *
     * Usage in template:
     *   @mouseenter="showTooltip('variables', 'pcr', undefined, undefined, $el)"
     *   @mouseleave="hideTooltip()"
     *   @touchstart.prevent="showTooltip('variables', 'pcr', undefined, undefined, $el)"
     */
    showTooltip(group: string, key: string, fallbackGroup?: string, fallbackKey?: string, el?: HTMLElement): void {
      // Cancel any pending hide
      if (this._tipHideTimer) {
        clearTimeout(this._tipHideTimer);
        this._tipHideTimer = null;
      }

      const data = (tooltipData as any)[group]?.[key]
        || (fallbackGroup && fallbackKey ? (tooltipData as any)[fallbackGroup]?.[fallbackKey] : null)
        || null;

      if (!data) return; // no tooltip data — do nothing

      this.tip.visible = true;
      this.tip.title = data.title || '';
      this.tip.data = data;

      // Position the tooltip — auto-flip direction based on available space
      if (el) {
        const rect = el.getBoundingClientRect();
        const spaceAbove = rect.top;
        const spaceBelow = window.innerHeight - rect.bottom;
        // Estimated max tooltip height: title + up to 2 sections + padding ≈ 220px
        const tipEstHeight = 220;
        // Flip below if not enough room above AND more room below
        const shouldFlip = spaceAbove < tipEstHeight && spaceBelow > spaceAbove;
        this.tip.dir = shouldFlip ? 'below' : 'above';
        this.tip.posX = rect.left + rect.width / 2;
        this.tip.posY = this.tip.dir === 'below' ? rect.bottom : rect.top;
      }
    },

    /**
     * Hide tooltip with a small delay (150ms) to handle:
     *   - Cursor briefly passing over gaps between elements
     *   - Rapid Alpine re-renders that temporarily unbind @mouseenter
     */
    hideTooltip(): void {
      // Use a short delay so the tooltip survives micro-renders
      if (this._tipHideTimer) clearTimeout(this._tipHideTimer);
      this._tipHideTimer = setTimeout(() => {
        this.tip.visible = false;
        this.tip.data = null;
        this._tipHideTimer = null;
      }, 150);
    },

    // ═══════════════════════════════════════
    //  STYLING HELPERS
    // ═══════════════════════════════════════

    dotColor(d: string): string {
      return DOT_COLOR_MAP[d] || DOT_COLOR_DEFAULT;
    },

    decisionSurfaceClass(d: string): string {
      return DECISION_SURFACE_MAP[d] || SURFACE_CLASS_DEFAULT;
    },

    // ═══════════════════════════════════════
    //  CLEANUP
    // ═══════════════════════════════════════

    destroy() {
      // Stop connecting timeout
      this._stopConnectingTimeout();

      // Clear all pending flash timers
      Object.values(this.assets).forEach((asset) => {
        const internal = asset as InternalAssetState;
        if (internal._flashTimer !== null) {
          clearTimeout(internal._flashTimer);
          internal._flashTimer = null;
        }
      });

      // Unsubscribe all WebSocket event listeners
      wsUnsubscribers.forEach((unsub) => unsub());
      wsUnsubscribers = [];

      // Unsubscribe from WS channels and disconnect
      // F1 FIX: pass activeBroker — not doing so defaults to 'binance'
      // which is wrong if user switched to another broker before navigating away.
      const ws = this.ws;
      if (ws) {
        const broker = this.activeBroker;
        this.activeUnderlyings.forEach((s: string) => {
          ws.unsubscribe(s, 'intelligence', broker);
          ws.unsubscribe(s, 'index', broker);
          ws.unsubscribe(s, 'indicators', broker);
        });
        ws.disconnect();
        this.ws = null;
      }
    },
  };

  return store;
}

// ── Cleanup on page unload ──
if (typeof window !== 'undefined') {
  window.addEventListener('beforeunload', () => {
    marketWS.disconnect();
  });
}

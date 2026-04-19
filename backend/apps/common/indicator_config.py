"""
Indicator & Strategy Configuration
====================================
Single source of truth for ALL indicator and strategy registration.
Kept separate from the engine logic for easy maintenance.

Every entry in INDICATOR_REGISTRY MUST have three matching pieces in
indicator_engine.py:  _calc_{name}()  (scalar),  _calc_series()  (chart series),
and a MIN_ROWS entry here.  See Section 4 in indicator_engine.py for the
full reference table.

Adding a new indicator:
  1. Add entry to INDICATOR_REGISTRY below (use slot() helpers!)
  2. Add MIN_ROWS entry if pandas-ta needs more than 5 rows
  3. Write _calc_{name}(df, **params) in indicator_engine.py  (scalar values)
  4. Add case in _calc_series(name, ...) in indicator_engine.py (chart series)
  5. Done — appears in Redis, WS broadcast, and frontend automatically.

Adding a new strategy:
  1. Define ta.Strategy(...) below
  2. Add entry to STRATEGY_REGISTRY below
  3. Add _decide_{key}() method in IndicatorEngine
  4. Done - strategy label appears in frontend automatically.

Slot builder reference:
  price_line(suffix, label, color, ...)   - price overlay (EMA/SMA/WMA/BB)
  osc_line(suffix, label, color, ...)     - oscillator sub-pane (RSI/ATR/Stoch/ADX)
  hist_slot(suffix, label, color, ...)    - histogram bars (MACD)
  area_slot(suffix, label, color, ...)    - area fill (Volume)
  line_slot(suffix, label, pane, color) - generic (use when above don't fit)

  Single-output:  use "label" + "chart_meta" directly.
  Multi-output:   use "multi": True, "slots": [slot(...), ...]
"""

from typing import Dict, List, Any, Optional


# ═══════════════════════════════════════════════════════════════════════════
# REUSABLE DEFAULTS
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_CLASS = "text-gray-300 dark:text-gray-400"
MUTED_CLASS = "text-gray-400/70"

# ═══════════════════════════════════════════════════════════════════════════
# SLOT BUILDERS — DRY helpers for indicator registration
# ═══════════════════════════════════════════════════════════════════════════
# Use these inside INDICATOR_REGISTRY to keep each indicator compact.
# Only specify what differs from defaults — everything else is filled in.
#
# Pane types:  "price_overlay" | "oscillator" | "histogram" | "area"
# Series types: "line" | "histogram" | "area"


def line_slot(
    suffix: str,
    label: str,
    pane: str,
    color: str,
    *,
    format: str = "decimal",
    decimals: int = 1,
    font_size: str = "11px",
    bold: bool = False,
    cls: str = DEFAULT_CLASS,
    y_range: list = [],
    ref_lines: list = [],
    line_width: float = 1,
    line_style: Optional[int] = None,
    color_rules: list = None,
) -> dict:
    """
    Generic line-series slot builder.
    Covers price_overlay, oscillator, and area panes.
    """
    chart_meta: dict = {
        "pane": pane,
        "series": "line" if pane != "area" else "area",
        "color": color,
        "y_range": y_range,
        "ref_lines": ref_lines,
        "line_width": line_width,
    }
    if line_style is not None:
        chart_meta["line_style"] = line_style

    slot: dict = {
        "suffix": suffix,
        "label": label,
        "format": format,
        "decimals": decimals,
        "font_size": font_size,
        "default_class": cls,
        "chart_meta": chart_meta,
    }
    if bold:
        slot["bold"] = True
    if color_rules:
        slot["color_rules"] = color_rules
    return slot


def hist_slot(
    suffix: str,
    label: str,
    color: str,
    *,
    format: str = "signed",
    decimals: int = 2,
    font_size: str = "10px",
    cls: str = DEFAULT_CLASS,
    ref_lines: list = [],
    color_rules: list = None,
) -> dict:
    """Histogram bar-series slot builder."""
    return {
        "suffix": suffix,
        "label": label,
        "format": format,
        "decimals": decimals,
        "font_size": font_size,
        "default_class": cls,
        "chart_meta": {
            "pane": "histogram",
            "series": "histogram",
            "color": color,
            "y_range": [],
            "ref_lines": ref_lines,
            "line_width": 1,
        },
        **({"color_rules": color_rules} if color_rules else {}),
    }


def area_slot(
    suffix: str,
    label: str,
    color: str,
    *,
    format: str = "volume",
    decimals: int = 0,
    font_size: str = "10px",
    cls: str = DEFAULT_CLASS,
) -> dict:
    """Area fill slot builder."""
    return {
        "suffix": suffix,
        "label": label,
        "format": format,
        "decimals": decimals,
        "font_size": font_size,
        "default_class": cls,
        "chart_meta": {
            "pane": "area",
            "series": "area",
            "color": color,
            "y_range": [],
            "ref_lines": [],
            "line_width": 1,
        },
    }


# ── Shorthand for common patterns ──────────────────────────────────────

def price_line(suffix: str, label: str, color: str, **kw) -> dict:
    """Price overlay line (EMA, SMA, WMA, BB, VWAP etc.)."""
    return line_slot(suffix, label, "price_overlay", color,
                     format="price", decimals=0, **kw)


def osc_line(suffix: str, label: str, color: str, **kw) -> dict:
    """Oscillator sub-pane line (RSI, Stoch K/D, ADX, ATR etc.)."""
    return line_slot(suffix, label, "oscillator", color, **kw)


# ═══════════════════════════════════════════════════════════════════════════
# MINIMUM DATA ROWS — checked BEFORE pandas-ta to suppress warnings
# ═══════════════════════════════════════════════════════════════════════════
# Format: {indicator_name: lambda params -> int}
# Must match every key in INDICATOR_REGISTRY.

MIN_ROWS: Dict[str, Any] = {
    # ── Price Overlay ──
    "ema":          lambda p: max(p.get("length", [9, 21])),
    "sma":          lambda p: max(p.get("length", [20, 50])),
    "wma":          lambda p: max(p.get("length", [44])),
    "bbands":       lambda p: p.get("length", 20),
    # ── Oscillator ──
    "rsi":          lambda p: p.get("length", 14),
    "atr":          lambda p: p.get("length", 14),
    "stoch":        lambda p: p.get("k", 14) + p.get("d", 3),
    "adx":          lambda p: p.get("length", 14) * 2,
    # ── Histogram ──
    "macd":         lambda p: p.get("slow", 26) + p.get("signal", 9),
    # ── Area ──
    "volume_sma":   lambda p: p.get("length", 20),
}

FALLBACK_MIN_ROWS: int = 5


# ═══════════════════════════════════════════════════════════════════════════
# TIMEFRAME DISPLAY CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

DISPLAY_TIMEFRAMES: List[str] = ["1m", "15m", "1h", "4h"]

TIMEFRAME_DISPLAY: Dict[str, Dict[str, str]] = {
    "1m":  {"label": "1m",  "color": "sky"},
    "15m": {"label": "15m", "color": "amber"},
    "1h":  {"label": "1h",  "color": "violet"},
    "4h":  {"label": "4h",  "color": "rose"},
}


# ═══════════════════════════════════════════════════════════════════════════
# INDICATOR REGISTRY
# ═══════════════════════════════════════════════════════════════════════════
#
# Organized by pane type for easy navigation.
# Each indicator has:
#   params      — passed to _calc_{name}(df, **params)
#   timeframes  — which TFs to calculate on (subset of DISPLAY_TIMEFRAMES)
#   order       — display order within a timeframe column (lower = top)
#   multi       — True  → use "slots" list for multiple outputs
#                False → use "label" + "chart_meta" for single output
#
# Slot suffixes MUST match the keys returned by _calc_series() in
# indicator_engine.py. See sync table at top of file.
#
# ┌─────────────────────────────────────────────────────────────────────────┐
# │  TO ADD A NEW INDICATOR: scroll to templates at bottom, copy, done.     │
# └─────────────────────────────────────────────────────────────────────────┘

INDICATOR_REGISTRY: Dict[str, Dict[str, Any]] = {

    # ═══════════════════════════════════════════════════════════════════════
    #  GROUP 1: PRICE OVERLAY — drawn on the candlestick price pane
    # ═══════════════════════════════════════════════════════════════════════
    #  Engine:    _calc_ema, _calc_sma, _calc_wma, _calc_bbands
    #  Series:    suffixes must be str(period) or "upper"/"mid"/"lower"
    # ═══════════════════════════════════════════════════════════════════════

    # ── EMA (Exponential Moving Average) ──────────────────────────
    "ema": {
        "timeframes": ["1m", "15m", "1h", "4h"],
        "params": {"length": [9, 21]},
        "order": 1,
        "multi": True,
        "slots": [
            price_line("9",  "EMA9",  "#fbbf24"),
            price_line("21", "EMA21", "#60a5fa"),
        ],
    },

    # ── SMA (Simple Moving Average) ───────────────────────────────
    "sma": {
        "timeframes": ["1m", "15m", "1h"],
        "params": {"length": [20, 50]},
        "order": 2,
        "multi": True,
        "slots": [
            price_line("20", "SMA20", "#f472b6"),
            price_line("50", "SMA50", "#c084fc"),
        ],
    },

    # ── WMA (Weighted Moving Average) ─────────────────────────────
    "wma": {
        "timeframes": ["1m", "15m", "1h", "4h"],
        "params": {"length": [44]},
        "order": 3,
        "multi": True,
        "slots": [
            price_line("44", "WMA44", "#fb923c"),
        ],
    },

    # ── Bollinger Bands (upper, mid, lower) ───────────────────────
    "bbands": {
        "timeframes": ["15m", "1h"],
        "params": {"length": 20, "std": 2},
        "order": 4,
        "multi": True,
        "slots": [
            price_line("upper", "BB-U", "#94a3b8", font_size="10px", line_style=2),
            price_line("mid",   "BB-M", "#64748b",  font_size="10px"),
            price_line("lower", "BB-L", "#94a3b8", font_size="10px", line_style=2),
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    #  GROUP 2: OSCILLATOR — drawn on dedicated oscillator sub-panes
    # ═══════════════════════════════════════════════════════════════════════
    #  Engine:    _calc_rsi, _calc_atr, _calc_stoch, _calc_adx
    #  Series:    single-output uses "default"; multi uses defined suffixes
    # ═══════════════════════════════════════════════════════════════════════

    # ── RSI (Relative Strength Index) ─────────────────────────────
    "rsi": {
        "timeframes": ["1m", "15m", "1h"],
        "params": {"length": 14},
        "order": 0,
        "label": "RSI",
        "format": "decimal",
        "decimals": 1,
        "bold": True,
        "font_size": "12px",
        "color_rules": [
            {"op": "gt", "value": 70, "class": "text-red-400"},
            {"op": "lt", "value": 30, "class": "text-emerald-400"},
        ],
        "default_class": "text-gray-200 dark:text-gray-300",
        "chart_meta": {
            "pane": "oscillator", "series": "line",
            "color": "#a78bfa", "y_range": [0, 100],
            "ref_lines": [30, 70], "line_width": 1,
        },
    },

    # ── ATR (Average True Range) ──────────────────────────────────
    "atr": {
        "timeframes": ["1m", "15m", "1h"],
        "params": {"length": 14},
        "order": 5,
        "label": "ATR",
        "format": "price",
        "decimals": 2,
        "font_size": "11px",
        "default_class": DEFAULT_CLASS,
        "chart_meta": {
            "pane": "oscillator", "series": "line",
            "color": "#f97316", "y_range": [],
            "ref_lines": [], "line_width": 1,
        },
    },

    # ── Stochastic Oscillator (K, D) ──────────────────────────────
    "stoch": {
        "timeframes": ["15m", "1h"],
        "params": {"k": 14, "d": 3, "smooth_k": 3},
        "order": 6,
        "multi": True,
        "slots": [
            osc_line("k", "StoK", "#22d3ee",
                     y_range=[0, 100], ref_lines=[20, 80], font_size="10px",
                     color_rules=[
                         {"op": "gt", "value": 80, "class": "text-red-400"},
                         {"op": "lt", "value": 20, "class": "text-emerald-400"},
                     ]),
            osc_line("d", "StoD", "#fb923c",
                     y_range=[0, 100], ref_lines=[20, 80], font_size="10px",
                     color_rules=[
                         {"op": "gt", "value": 80, "class": "text-red-400"},
                         {"op": "lt", "value": 20, "class": "text-emerald-400"},
                     ]),
        ],
    },

    # ── ADX (Average Directional Index) ───────────────────────────
    "adx": {
        "timeframes": ["1m", "15m", "1h"],
        "params": {"length": 14},
        "order": 7,
        "multi": True,
        "slots": [
            osc_line("adx", "ADX",  "#facc15",
                     y_range=[0, 60], ref_lines=[25], line_width=1.5,
                     color_rules=[{"op": "gt", "value": 25, "class": "text-amber-400"}]),
            osc_line("dmp", "+DI",  "#34d399", y_range=[0, 60],
                     font_size="10px", cls=MUTED_CLASS),
            osc_line("dmn", "-DI",  "#f87171", y_range=[0, 60],
                     font_size="10px", cls=MUTED_CLASS),
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    #  GROUP 3: HISTOGRAM — drawn on dedicated histogram sub-panes
    # ═══════════════════════════════════════════════════════════════════════
    #  Engine:    _calc_macd
    #  Series:    suffixes "hist", "line", "signal" all share histogram pane
    # ═══════════════════════════════════════════════════════════════════════

    # ── MACD (Moving Average Convergence Divergence) ──────────────
    "macd": {
        "timeframes": ["1h", "4h"],
        "params": {"fast": 12, "slow": 26, "signal": 9},
        "order": 8,
        "multi": True,
        "slots": [
            hist_slot("hist",   "MACD",   "#4ade80", ref_lines=[0],
                      color_rules=[
                          {"op": "gt", "value": 0, "class": "text-emerald-400"},
                          {"op": "lt", "value": 0, "class": "text-red-400"},
                      ]),
            # line/signal share the histogram pane
            line_slot("line",   "MACD-L", "histogram", "#60a5fa",
                      format="signed", decimals=2, font_size="10px"),
            line_slot("signal", "MACD-S", "histogram", "#f97316",
                      format="signed", decimals=2, font_size="10px"),
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    #  GROUP 4: AREA — drawn on dedicated area sub-panes
    # ═══════════════════════════════════════════════════════════════════════
    #  Engine:    _calc_volume_sma
    #  Series:    single-output uses "default"
    # ═══════════════════════════════════════════════════════════════════════

    # ── Volume SMA ────────────────────────────────────────────────
    "volume_sma": {
        "timeframes": ["1h"],
        "params": {"length": 20},
        "order": 9,
        "label": "VolSM",
        "format": "volume",
        "decimals": 0,
        "font_size": "10px",
        "default_class": DEFAULT_CLASS,
        "chart_meta": {
            "pane": "area", "series": "area",
            "color": "#6366f1", "y_range": [],
            "ref_lines": [], "line_width": 1,
        },
    },

    # ═══════════════════════════════════════════════════════════════════════
    #  TEMPLATES — copy one, tweak, done!
    # ═══════════════════════════════════════════════════════════════════════

    # ── TEMPLATE: Multi-period price overlay (EMA/SMA/WMA pattern) ──
    # "your_ma": {
    #     "timeframes": ["1m", "15m", "1h", "4h"],
    #     "params": {"length": [10, 30]},
    #     "order": 10,
    #     "multi": True,
    #     "slots": [
    #         price_line("10", "Ind10", "#fbbf24"),
    #         price_line("30", "Ind30", "#60a5fa"),
    #     ],
    # },

    # ── TEMPLATE: Single-output oscillator ────────────────────────
    # "your_osc": {
    #     "timeframes": ["15m", "1h"],
    #     "params": {"length": 14},
    #     "order": 10,
    #     "label": "YourOsc",
    #     "format": "decimal",
    #     "decimals": 1,
    #     "bold": False,
    #     "font_size": "11px",
    #     "default_class": DEFAULT_CLASS,
    #     "chart_meta": {
    #         "pane": "oscillator", "series": "line",
    #         "color": "#a78bfa", "y_range": [],
    #         "ref_lines": [], "line_width": 1,
    #     },
    # },

    # ── TEMPLATE: Multi-output oscillator (Stoch/ADX pattern) ─────
    # "your_osc_multi": {
    #     "timeframes": ["15m", "1h"],
    #     "params": {"fast": 14, "slow": 3},
    #     "order": 10,
    #     "multi": True,
    #     "slots": [
    #         osc_line("fast", "Fast", "#22d3ee",
    #                  y_range=[0, 100], ref_lines=[20, 80],
    #                  color_rules=[...]),
    #         osc_line("slow", "Slow", "#fb923c",
    #                  y_range=[0, 100], ref_lines=[20, 80]),
    #     ],
    # },

    # ── TEMPLATE: Histogram (MACD pattern) ────────────────────────
    # "your_hist": {
    #     "timeframes": ["1h", "4h"],
    #     "params": {"fast": 5, "slow": 20},
    #     "order": 10,
    #     "multi": True,
    #     "slots": [
    #         hist_slot("hist", "Name", "#4ade80", ref_lines=[0],
    #                   color_rules=[...]),
    #         line_slot("line", "Name-L", "histogram", "#60a5fa",
    #                   format="signed", decimals=2),
    #     ],
    # },

    # ── TEMPLATE: Area fill (Volume pattern) ──────────────────────
    # "your_area": {
    #     "timeframes": ["1h"],
    #     "params": {"length": 20},
    #     "order": 10,
    #     "label": "YourArea",
    #     "format": "volume",
    #     "decimals": 0,
    #     "font_size": "10px",
    #     "default_class": DEFAULT_CLASS,
    #     "chart_meta": {
    #         "pane": "area", "series": "area",
    #         "color": "#6366f1", "y_range": [],
    #         "ref_lines": [], "line_width": 1,
    #     },
    # },
}


# ═══════════════════════════════════════════════════════════════════════════
# STRATEGY DEFINITIONS (pandas-ta Strategy Pattern)
# ═══════════════════════════════════════════════════════════════════════════
# NOTE: These require `pandas_ta_classic as ta` — imported inside
# indicator_engine.py to avoid circular imports.
#
# Each strategy:
#   - Composes indicators via ta.Strategy(..., ta=[...])
#   - Runs via df.ta.strategy() on its preferred timeframe
#   - Values extracted and passed to _decide_{key}() method
#   - Multi-timeframe strategies use multi_timeframe=True instead

STRATEGY_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "MomentumStrategy": {
        "name": "Momentum",
        "description": "Short-term momentum: RSI + MACD + Stochastic",
        "ta": [
            {"kind": "rsi", "length": 14},
            {"kind": "macd", "fast": 12, "slow": 26, "signal": 9},
            {"kind": "stoch", "k": 14, "d": 3, "smooth_k": 3},
        ],
    },
    "TrendStrategy": {
        "name": "Trend",
        "description": "Trend direction and strength: EMA stack + SMA50 + ADX",
        "ta": [
            {"kind": "ema", "length": 9},
            {"kind": "ema", "length": 21},
            {"kind": "sma", "length": 50},
            {"kind": "adx", "length": 14},
        ],
    },
    "VolatilityStrategy": {
        "name": "Volatility",
        "description": "Mean reversion: Bollinger Bands position + ATR",
        "ta": [
            {"kind": "bbands", "length": 20},
            {"kind": "atr", "length": 14},
        ],
    },
}

# Strategy registry entries
STRATEGY_REGISTRY: List[Dict[str, Any]] = [
    # ── Single-timeframe strategies (use df.ta.strategy) ──
    {
        "key": "momentum",
        "strategy_name": "MomentumStrategy",
        "timeframes": ["1h", "15m"],
        "display": {"label": "Mom", "order": 0},
    },
    {
        "key": "trend",
        "strategy_name": "TrendStrategy",
        "timeframes": ["4h", "1h"],
        "display": {"label": "Trend", "order": 1},
    },
    {
        "key": "volatility",
        "strategy_name": "VolatilityStrategy",
        "timeframes": ["1h", "15m"],
        "display": {"label": "Vol", "order": 2},
    },

    # ── Multi-timeframe strategies ──────────────────────────────
    {
        "key": "fast_trend",
        "timeframes": ["5m", "15m", "4h"],
        "display": {"label": "FST", "order": 3},
        "multi_timeframe": True,
    },
    {
        "key": "session_breakout",
        "timeframes": ["1m", "5m", "1h"],
        "display": {"label": "SBS", "order": 4},
        "multi_timeframe": True,
    },

    # ── ADD YOUR NEW STRATEGIES BELOW ──────────────────────────
]

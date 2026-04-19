# Adding New Indicators — Step-by-Step Guide

This guide covers how to add a new technical indicator to the MarketPulse chart system. Adding an indicator requires changes in **two files**:

| File | Purpose |
|------|---------|
| `indicator_config.py` | Register the indicator: parameters, timeframes, display slots, and minimum data rows |
| `indicator_engine.py` | Implement the calculation: scalar values for the grid + time-series for charts |

No frontend changes are needed. The `chartRenderer.ts` reads `chart_meta` from the backend and renders dynamically — it has zero indicator-specific logic.

---

## Table of Contents

1. [Quick Overview](#1-quick-overview)
2. [File Locations](#2-file-locations)
3. [Step-by-Step: Add a New Indicator](#3-step-by-step-add-a-new-indicator)
4. [Indicator Types Reference](#4-indicator-types-reference)
5. [Slot Builder Cheat Sheet](#5-slot-builder-cheat-sheet)
6. [Step-by-Step: Add a New Strategy](#6-step-by-step-add-a-new-strategy)
7. [Data Flow Diagram](#7-data-flow-diagram)
8. [Common Patterns & Examples](#8-common-patterns--examples)
9. [Troubleshooting Checklist](#9-troubleshooting-checklist)

---

## 1. Quick Overview

### The Two-File Contract

Every indicator has a **registry key** (e.g., `"rsi"`, `"ema"`, `"macd"`). This key links three things:

```
indicator_config.py                    indicator_engine.py
┌─────────────────────┐               ┌─────────────────────────┐
│ INDICATOR_REGISTRY   │               │ _calc_{key}()           │
│   "rsi": {          │── key ───────▶│ _calc_series()          │
│     params: {...},  │               │   (if/elif name == key) │
│     slots: [...],   │               │ MIN_ROWS (in config)    │
│   }                 │               │                         │
└─────────────────────┘               └─────────────────────────┘
         │                                       │
         ▼                                       ▼
    Frontend receives                         Backend calculates
    display config + values                   values + series data
```

### The 5 Steps

1. **Choose an indicator type** (price overlay, oscillator, histogram, area)
2. **Add `MIN_ROWS` entry** in `indicator_config.py`
3. **Add registry entry** in `INDICATOR_REGISTRY` with slots
4. **Add `_calc_{name}()`** method in `IndicatorEngine` (scalar values)
5. **Add case in `_calc_series()`** in `IndicatorEngine` (chart series)

---

## 2. File Locations

```
backend/apps/common/
├── indicator_config.py      ← Registry, slot builders, MIN_ROWS, strategies
├── indicator_engine.py      ← Calculation engine, _calc_* methods
├── session_breakout.py      ← Standalone strategy (SBS)
└── INDICATOR_GUIDE.md       ← This file

frontend/src/utils/
└── chartRenderer.ts         ← NO TOUCHING — reads chart_meta dynamically
```

---

## 3. Step-by-Step: Add a New Indicator

### Example: Adding CCI (Commodity Channel Index)

CCI is a bounded oscillator (oscillates around 0, typically -200 to +200). We will display it on an oscillator sub-pane.

---

#### Step 1: Choose the Indicator Type

| Type | Pane | Slot Builder | When to Use |
|------|------|-------------|-------------|
| Price Overlay | `price_overlay` | `price_line()` | Indicators plotted on the candlestick chart (MA, BB, VWAP) |
| Oscillator | `oscillator` | `osc_line()` | Bounded/semi-bounded sub-pane (RSI, Stoch, ATR, ADX) |
| Histogram | `histogram` | `hist_slot()` + `line_slot()` | Bar-style sub-pane (MACD) |
| Area | `area` | `area_slot()` | Filled area sub-pane (Volume) |

CCI → **Oscillator** → use `osc_line()`

---

#### Step 2: Add MIN_ROWS in `indicator_config.py`

Find the `MIN_ROWS` dict (around line 184). Add your entry in the appropriate group section:

```python
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
    "cci":          lambda p: p.get("length", 20),          # ← NEW
    # ── Histogram ──
    "macd":         lambda p: p.get("slow", 26) + p.get("signal", 9),
    # ── Area ──
    "volume_sma":   lambda p: p.get("length", 20),
}
```

**What is MIN_ROWS?** It is the minimum number of candle rows needed before pandas-ta can produce a valid value. The engine checks this *before* calling pandas-ta to suppress warnings. If omitted, a fallback of 5 is used.

**Rule of thumb:** For most indicators, `MIN_ROWS = p.get("length", default)` is sufficient. For indicators with multiple params that combine (like Stoch K+D or MACD slow+signal), sum them. For ADX which internally needs `length * 2`, use that formula.

---

#### Step 3: Add Registry Entry in `indicator_config.py`

Find the `INDICATOR_REGISTRY` dict. Add your entry in the correct group section.

**CCI is single-output**, so we use the `label` + `chart_meta` pattern (no `multi: True`):

```python
INDICATOR_REGISTRY: Dict[str, Dict[str, Any]] = {

    # ... existing entries ...

    # ═══════════════════════════════════════════════════════════
    #  GROUP 2: OSCILLATOR
    # ═══════════════════════════════════════════════════════════

    # ── CCI (Commodity Channel Index) ───────────────────────
    "cci": {
        "timeframes": ["15m", "1h"],         # Which timeframes to show on
        "params": {"length": 20},             # Passed to _calc_cci(df, length=20)
        "order": 10,                          # Display order (lower = higher in list)
        "label": "CCI",                       # Shown in the indicator grid cell
        "format": "decimal",                  # How to format the scalar value
        "decimals": 1,                        # Decimal places
        "bold": False,                        # Bold text in grid
        "font_size": "11px",                  # Font size in grid
        "default_class": DEFAULT_CLASS,       # Tailwind class for default state
        "color_rules": [                      # Conditional coloring in grid
            {"op": "gt", "value": 100, "class": "text-red-400"},
            {"op": "lt", "value": -100, "class": "text-emerald-400"},
        ],
        "chart_meta": {                       # ← DRIVES FRONTEND RENDERING
            "pane": "oscillator",             #   Which pane to draw on
            "series": "line",                 #   Line type (line/histogram/area)
            "color": "#818cf8",              #   Line color
            "y_range": [],                    #   Fixed y-axis range (empty = auto)
            "ref_lines": [0, 100, -100],      #   Horizontal reference lines
            "line_width": 1,                  #   Line thickness
        },
    },

    # ... rest of entries ...
}
```

**Key fields explained:**

| Field | Required | Description |
|-------|----------|-------------|
| `timeframes` | Yes | List of timeframe keys from `DISPLAY_TIMEFRAMES`. Only these TFs will calculate this indicator. |
| `params` | Yes | Dict passed as `**params` to `_calc_{name}(df, **params)`. Also used by MIN_ROWS lambda. |
| `order` | Yes | Display order within a timeframe column. Lower = appears higher. Use gaps (5, 10, 15) to leave room for future insertions. |
| `label` | Single only | Short label for the indicator grid (e.g., "RSI", "CCI"). |
| `multi` | No | Set `True` if indicator has multiple output slots (e.g., MACD has line/signal/hist). |
| `slots` | Multi only | List of slot dicts (built with `price_line()`, `osc_line()`, etc.). Each slot has its own `suffix`, `label`, `chart_meta`. |
| `chart_meta` | Yes | The rendering specification consumed by `chartRenderer.ts`. This is the **bridge** between backend and frontend. |
| `format` | Yes | One of: `"decimal"`, `"price"`, `"signed"`, `"volume"` — controls how the scalar value is formatted in the grid. |
| `color_rules` | No | List of `{op, value, class}` rules for conditional grid coloring. |

**If your indicator is multi-output** (like MACD with line/signal/histogram), use the template approach:

```python
# Multi-output example pattern:
"my_indicator": {
    "timeframes": ["1h", "4h"],
    "params": {"fast": 5, "slow": 20},
    "order": 10,
    "multi": True,                         # ← Required for multi-output
    "slots": [
        osc_line("fast", "Fast", "#22d3ee", y_range=[0, 100]),
        osc_line("slow", "Slow", "#fb923c", y_range=[0, 100]),
    ],
},
```

The `multi` flag tells the engine to iterate `slots` and generate a key per slot: `{tf}_{name}_{slot_suffix}`.

---

#### Step 4: Add `_calc_{name}()` in `indicator_engine.py`

Navigate to **SECTION 4: INDICATOR IMPLEMENTATIONS** (around line 1050) in `indicator_engine.py`. Add your method in the correct group subsection:

```python
    # ── Oscillator ───────────────────────────────────────────────────────

    def _calc_cci(self, df: pd.DataFrame, length: int = 20) -> Optional[float]:
        """Commodity Channel Index — single output."""
        result = ta.cci(df["high"], df["low"], df["close"], length=length)
        if result is not None and len(result) > 0:
            return float(result.iloc[-1])
        return None
```

**Return type rules:**

| Indicator Type | Return from `_calc_{name}()` | Example |
|----------------|-------------------------------|---------|
| Single-output | `Optional[float]` (just the last value, or `None`) | `return float(result.iloc[-1])` |
| Multi-output | `Dict[str, float]` (suffix → last value) | `return {"k": 62.4, "d": 58.1}` |

**For multi-output**, the dict keys MUST match the `suffix` values in your config slots. For example, if your slots have `suffix="k"` and `suffix="d"`, your `_calc_stoch` must return `{"k": val_k, "d": val_d}`.

**Parameters:** The method signature must accept `**params` matching your config's `params` dict. The engine calls `handler(df, **params)`, so if your config has `{"length": 20}`, your method should have `length: int = 20` as a keyword argument.

---

#### Step 5: Add Case in `_calc_series()` in `indicator_engine.py`

Navigate to the `_calc_series()` method (around line 896). This method produces the full time-series data for chart rendering. Add an `elif` branch:

```python
    def _calc_series(self, name, df, config, params):
        try:
            if name == "rsi":
                s = ta.rsi(df["close"], length=params.get("length", 14))
                return {"default": s} if s is not None else None

            # ... existing branches ...

            elif name == "cci":
                s = ta.cci(df["high"], df["low"], df["close"],
                           length=params.get("length", 20))
                return {"default": s} if s is not None else None

            else:
                return None
```

**Critical: The suffix key must match!**

| Config Pattern | `_calc_series()` Return Key |
|----------------|---------------------------|
| Single-output (`multi: false`) | Always `{"default": pd.Series}` |
| Multi-output slot with `suffix="k"` | `{"k": pd.Series}` |
| Multi-output slot with `suffix="line"` | `{"line": pd.Series}` |

The engine matches config slot `suffix` → `_calc_series()` return key → produces chart series key `{tf}_{indicator}_{suffix}`.

**For multi-output indicators with multiple periods** (like EMA with periods [9, 21]):

```python
elif name == "ema":
    out = {}
    for period in params.get("length", [9, 21]):
        s = ta.ema(df["close"], length=period)
        if s is not None:
            out[str(period)] = s    # suffix must be str(period) e.g. "9", "21"
    return out or None
```

---

#### Done!

After these 5 steps, the indicator will automatically appear in:

- **Redis** hash `indicators:{SYMBOL}` as scalar values (e.g., `15m_cci_20: 42.5`)
- **WebSocket broadcast** as part of `calculate_all_with_strategy()` response
- **Indicator grid** in the frontend (label, formatting, color rules)
- **Chart rendering** as a line/histogram/area on the correct sub-pane

**No frontend changes needed** — `chartRenderer.ts` reads `chart_meta` from the config and plots the `{time, value}` series data directly.

---

## 4. Indicator Types Reference

### Price Overlay

Drawn directly on the candlestick chart. Used for moving averages, bands, and price-level indicators.

**Config pattern:** Use `price_line()` slot builder or `multi: True` with multiple `price_line()` slots.

**Engine pattern:** Always multi-output. Returns `{suffix: float}` where suffix is the period or band component.

**Available pandas-ta inputs:** `df["close"]` only (or `df["high"]`, `df["low"]`, `df["close"]` for BBands).

**Examples:** EMA, SMA, WMA, Bollinger Bands, VWAP, Supertrend

### Oscillator

Drawn on a dedicated sub-pane below the price chart. Used for momentum, volatility, and strength indicators.

**Config pattern:** Use `osc_line()` slot builder. Supports `y_range` (fixed axis), `ref_lines` (horizontal markers), and `color_rules`.

**Engine pattern:** Single-output returns `Optional[float]`. Multi-output returns `{suffix: float}`.

**Available pandas-ta inputs:** Varies by indicator. Most use `df["close"]`, some use HLC, and ATR uses HLC.

**Examples:** RSI, ATR, Stochastic, ADX, CCI, Williams %R, MFI

### Histogram

Drawn as bars on a dedicated sub-pane. Used for divergence/momentum indicators.

**Config pattern:** Use `hist_slot()` for the main bars + `line_slot(..., pane="histogram")` for signal lines. The `hist_slot()` bars automatically get green/red coloring based on positive/negative values.

**Engine pattern:** Always multi-output. Returns `{suffix: float}` with suffixes like `"hist"`, `"line"`, `"signal"`.

**Examples:** MACD, MACD histogram

### Area

Drawn as a filled area on a dedicated sub-pane. Used for volume-type indicators.

**Config pattern:** Use `area_slot()` for a single filled series.

**Engine pattern:** Single-output returns `Optional[float]`.

**Available pandas-ta inputs:** `df["volume"]` typically.

**Examples:** Volume SMA, OBV

---

## 5. Slot Builder Cheat Sheet

All builders are defined in `indicator_config.py` (around line 56). Use these to keep indicator entries compact:

### `price_line(suffix, label, color, **kw)`

```python
price_line("9", "EMA9", "#fbbf24")
price_line("50", "SMA50", "#c084fc", line_style=2)   # dashed line
```

Defaults: `pane="price_overlay"`, `format="price"`, `decimals=0`

### `osc_line(suffix, label, color, **kw)`

```python
osc_line("k", "StoK", "#22d3ee", y_range=[0, 100], ref_lines=[20, 80])
osc_line("adx", "ADX", "#facc15", y_range=[0, 60], ref_lines=[25],
         color_rules=[{"op": "gt", "value": 25, "class": "text-amber-400"}])
```

Defaults: `pane="oscillator"`, `format="decimal"`, `decimals=1`

### `hist_slot(suffix, label, color, **kw)`

```python
hist_slot("hist", "MACD", "#4ade80", ref_lines=[0],
          color_rules=[
              {"op": "gt", "value": 0, "class": "text-emerald-400"},
              {"op": "lt", "value": 0, "class": "text-red-400"},
          ])
```

Defaults: `pane="histogram"`, `series="histogram"`, `format="signed"`, `decimals=2`

### `area_slot(suffix, label, color, **kw)`

```python
area_slot("default", "VolSM", "#6366f1")
```

Defaults: `pane="area"`, `series="area"`, `format="volume"`, `decimals=0`

### `line_slot(suffix, label, pane, color, **kw)`

Generic builder when the above do not fit. Commonly used for MACD signal/line series that share the histogram pane:

```python
line_slot("line", "MACD-L", "histogram", "#60a5fa", format="signed", decimals=2)
line_slot("signal", "MACD-S", "histogram", "#f97316", format="signed", decimals=2)
```

### Available `**kw` Parameters for All Builders

| Parameter | Default | Description |
|-----------|---------|-------------|
| `format` | Varies | `"decimal"`, `"price"`, `"signed"`, `"volume"` |
| `decimals` | Varies | Number of decimal places |
| `font_size` | `"11px"` | Grid cell font size |
| `bold` | `False` | Bold text in grid |
| `cls` | `DEFAULT_CLASS` | Tailwind CSS class for grid cell |
| `y_range` | `[]` | Fixed y-axis range (e.g., `[0, 100]` for RSI). Empty = auto-scale. |
| `ref_lines` | `[]` | Horizontal reference line values (e.g., `[30, 70]` for RSI) |
| `line_width` | `1` | Line thickness (1 = thin, 2 = medium, 1.5 = between) |
| `line_style` | `None` | Line style: `0` = solid, `1` = dotted, `2` = dashed |
| `color_rules` | `None` | Conditional grid coloring rules |

---

## 6. Step-by-Step: Add a New Strategy

Strategies are separate from indicators — they compose existing indicators into trading signals.

### Single-Timeframe Strategy

**Step 1:** Add `ta.Strategy` definition in `indicator_config.py` under `STRATEGY_DEFINITIONS`:

```python
STRATEGY_DEFINITIONS = {
    # ... existing ...

    "MyNewStrategy": {
        "name": "MyNew",
        "description": "Description of the strategy logic",
        "ta": [
            {"kind": "rsi", "length": 14},
            {"kind": "cci", "length": 20},
        ],
    },
}
```

**Step 2:** Add entry in `STRATEGY_REGISTRY`:

```python
STRATEGY_REGISTRY = [
    # ... existing ...
    {
        "key": "my_new",
        "strategy_name": "MyNewStrategy",
        "timeframes": ["1h", "15m"],
        "display": {"label": "New", "order": 5},
    },
]
```

**Step 3:** Add `_decide_{key}()` method in `IndicatorEngine`:

```python
def _decide_my_new(self, values: dict, price: float = None) -> dict:
    """
    My New Strategy Decision.
    Uses RSI and CCI.

    Rules:
      RSI < 30 AND CCI < -100 = strongly bullish (2 pts)
      RSI > 70 AND CCI > 100  = strongly bearish (2 pts)
      RSI < 30 OR CCI < -100  = mild bull (1 pt)
      RSI > 70 OR CCI > 100   = mild bear (1 pt)
    """
    bull, bear, matched = 0, 0, []

    rsi = _find_value(values, ["RSI_"])
    cci = _find_value(values, ["CCI_"])

    if rsi is not None:
        if rsi < 30:
            bull += 1
            matched.append("RSI_oversold")
        elif rsi > 70:
            bear += 1
            matched.append("RSI_overbought")

    if cci is not None:
        if cci < -100:
            bull += 1
            matched.append("CCI_oversold")
        elif cci > 100:
            bear += 1
            matched.append("CCI_overbought")

    label, color = _classify_decision(bull, bear)
    return {
        "label": label, "color": color,
        "bull_score": bull, "bear_score": bear,
        "rules_matched": matched,
    }
```

### Multi-Timeframe Strategy

**Step 1:** Add to `STRATEGY_REGISTRY` with `multi_timeframe: True`:

```python
{
    "key": "my_mtf_strategy",
    "timeframes": ["5m", "15m", "4h"],
    "display": {"label": "MTF", "order": 6},
    "multi_timeframe": True,        # ← No strategy_name needed
},
```

**Step 2:** Add `_decide_mtf_{key}()` method (receives full `candles_by_tf`):

```python
def _decide_mtf_my_mtf_strategy(
    self, candles_by_tf: Dict[str, List[Dict]], price: float = None
) -> dict:
    """
    Multi-TF Strategy — receives full candles_by_tf.
    Compute indicators directly using ta.* functions.
    """
    bull, bear, matched = 0, 0, []

    # Compute your indicators across timeframes
    candles_5m = candles_by_tf.get("5m", [])
    # ... your logic ...

    label, color = _classify_decision(bull, bear)
    return {
        "label": label, "color": color,
        "bull_score": bull, "bear_score": bear,
        "rules_matched": matched,
    }
```

---

## 7. Data Flow Diagram

```
                    ┌──────────────────────┐
                    │   INDICATOR_REGISTRY │  (indicator_config.py)
                    │   + MIN_ROWS         │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   IndicatorEngine    │  (indicator_engine.py)
                    │                      │
                    │  calculate_all()     │ ──▶ Redis hash + scalar values
                    │  calculate_all_      │ ──▶ {time, value} series
                    │    series()          │
                    │  compute_strategies()│ ──▶ Strategy decisions
                    │  get_display_config()│ ──▶ Frontend rendering spec
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   REST API / WS      │
                    │   /api/market/       │
                    │   indicator-series/  │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   Frontend           │
                    │   chartRenderer.ts   │  ← ZERO indicator logic
                    │   Reads chart_meta   │     just plots {time, value}
                    │   Plots series data  │
                    └──────────────────────┘
```

### Key Format: `{timeframe}_{indicator}_{suffix}`

Examples:
- `1m_rsi_14` — RSI on 1m TF, single-output (suffix = param string "14")
- `1h_ema_9` — EMA period 9 on 1h TF (multi-output, suffix = "9")
- `4h_macd_hist` — MACD histogram on 4h TF (multi-output, suffix = "hist")
- `15m_bbands_upper` — Bollinger upper band on 15m TF (multi-output, suffix = "upper")

---

## 8. Common Patterns & Examples

### Pattern A: Single-Period Moving Average (like EMA)

**Config (single or multi-period):**
```python
"hma": {                           # Hull Moving Average
    "timeframes": ["1m", "15m", "1h", "4h"],
    "params": {"length": [20, 50]},
    "order": 11,
    "multi": True,
    "slots": [
        price_line("20", "HMA20", "#a78bfa"),
        price_line("50", "HMA50", "#e879f9"),
    ],
},
```

**Engine — scalar:**
```python
def _calc_hma(self, df, length=None):
    if not length:
        length = [20, 50]
    results = {}
    for period in length:
        result = ta.hma(df["close"], length=period)
        if result is not None and len(result) > 0:
            val = float(result.iloc[-1])
            if val is not None:
                results[str(period)] = val
    return results
```

**Engine — series:**
```python
elif name == "hma":
    out = {}
    for period in params.get("length", [20, 50]):
        s = ta.hma(df["close"], length=period)
        if s is not None:
            out[str(period)] = s
    return out or None
```

**MIN_ROWS:**
```python
"hma": lambda p: max(p.get("length", [20, 50])) * 2,
```

---

### Pattern B: Multi-Component Oscillator (like Stochastic)

**Config:**
```python
"willr": {                         # Williams %R
    "timeframes": ["15m", "1h"],
    "params": {"length": 14},
    "order": 11,
    "label": "WR",
    "format": "decimal",
    "decimals": 1,
    "font_size": "11px",
    "default_class": DEFAULT_CLASS,
    "chart_meta": {
        "pane": "oscillator", "series": "line",
        "color": "#f472b6", "y_range": [-100, 0],
        "ref_lines": [-20, -80], "line_width": 1,
    },
},
```

**Engine — scalar:**
```python
def _calc_willr(self, df, length=14):
    result = ta.willr(df["high"], df["low"], df["close"], length=length)
    if result is not None and len(result) > 0:
        return float(result.iloc[-1])
    return None
```

**Engine — series:**
```python
elif name == "willr":
    s = ta.willr(df["high"], df["low"], df["close"],
                  length=params.get("length", 14))
    return {"default": s} if s is not None else None
```

**MIN_ROWS:**
```python
"willr": lambda p: p.get("length", 14),
```

---

### Pattern C: Indicator with pandas-ta Multi-Column Output (like BBands)

When pandas-ta returns a DataFrame with column names like `BBL_20_2.0`, `BBM_20_2.0`, `BBU_20_2.0`, you need to map them to your suffix names:

```python
# Scalar method
def _calc_bbands(self, df, length=20, std=2):
    result = ta.bbands(df["close"], length=length, std=std)
    if result is not None:
        row = result.iloc[-1]
        results = {}
        for col in result.columns:
            if col.startswith("BBL"):
                results["lower"] = float(row[col])
            elif col.startswith("BBM"):
                results["mid"] = float(row[col])
            elif col.startswith("BBU"):
                results["upper"] = float(row[col])
        return results
    return {}

# Series method (same column mapping logic)
elif name == "bbands":
    result = ta.bbands(df["close"], length=params.get("length", 20),
                        std=params.get("std", 2))
    if result is None:
        return None
    out = {}
    for col in result.columns:
        if col.startswith("BBL"):
            out["lower"] = result[col]
        elif col.startswith("BBM"):
            out["mid"] = result[col]
        elif col.startswith("BBU"):
            out["upper"] = result[col]
    return out or None
```

**Tip:** To discover pandas-ta column names, run `ta.bbands(df["close"], length=20).columns` and inspect the output.

---

## 9. Troubleshooting Checklist

### Indicator not appearing in chart?

- [ ] Check `INDICATOR_REGISTRY` entry exists with correct key
- [ ] Check `timeframes` list includes the timeframe you're viewing
- [ ] Check `_calc_{name}()` method exists in `IndicatorEngine`
- [ ] Check `_calc_series()` has a matching `elif name == "..."` branch
- [ ] Check suffix names match between config `slots[].suffix` and `_calc_series()` return keys
- [ ] Check `MIN_ROWS` — if set too high, indicator may be skipped due to insufficient data
- [ ] Check browser console for errors
- [ ] Check backend logs for `No handler for indicator` warning

### Chart pane is empty?

- [ ] Verify `chart_meta.pane` is correct (`"price_overlay"`, `"oscillator"`, `"histogram"`, or `"area"`)
- [ ] Verify `chart_meta.series` matches the pane type (`"line"`, `"histogram"`, `"area"`)
- [ ] Check if `y_range` is set incorrectly (e.g., `[0, 100]` but values exceed that range)
- [ ] Check if backend is actually returning series data (inspect `/api/market/indicator-series/` response)

### Scalar value shows "—" in grid?

- [ ] Check if there's enough candle data (at least `MIN_ROWS` value)
- [ ] Check if the pandas-ta call is returning `None` (insufficient data or unsupported parameters)
- [ ] Check backend logs for debug messages about skipping

### Multiple indicators sharing the same sub-pane overlap?

- [ ] Sub-panes are shared by `pane` type. All oscillators share one pane, all histograms share one pane, etc.
- [ ] If you need a separate pane, use a different `pane` name (e.g., `"oscillator_2"`) — but note the frontend only handles the 4 standard pane types.

### Want to remove an indicator?

1. Remove the entry from `INDICATOR_REGISTRY`
2. Remove the `MIN_ROWS` entry
3. Optionally remove `_calc_{name}()` and the `_calc_series()` branch (or leave them — they won't be called if not in the registry)
4. If the indicator is used in any strategy, update the strategy accordingly

---

## Appendix: Current Indicator Summary

| Key | Type | Multi | Timeframes | pandas-ta Function | Input Columns |
|-----|------|-------|------------|--------------------|---------------|
| `ema` | price_overlay | Yes (9, 21) | 1m, 15m, 1h, 4h | `ta.ema()` | close |
| `sma` | price_overlay | Yes (20, 50) | 1m, 15m, 1h | `ta.sma()` | close |
| `wma` | price_overlay | Yes (44) | 1m, 15m, 1h, 4h | `ta.wma()` | close |
| `bbands` | price_overlay | Yes (upper/mid/lower) | 15m, 1h | `ta.bbands()` | close |
| `rsi` | oscillator | No | 1m, 15m, 1h | `ta.rsi()` | close |
| `atr` | oscillator | No | 15m, 1h | `ta.atr()` | high, low, close |
| `stoch` | oscillator | Yes (k, d) | 15m, 1h | `ta.stoch()` | high, low, close |
| `adx` | oscillator | Yes (adx, dmp, dmn) | 1h, 4h | `ta.adx()` | high, low, close |
| `macd` | histogram | Yes (line, signal, hist) | 1h, 4h | `ta.macd()` | close |
| `volume_sma` | area | No | 1h | `ta.sma()` | volume |

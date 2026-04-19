"""
Indicator Engine — Modular Technical Analysis
==============================================
Calculates technical indicators from OHLCV candle data using pandas-ta.
Completely independent from the 7-variable intelligence engine.

Architecture:
  - INDICATOR_REGISTRY defines WHAT to calculate and HOW to display it.
  - STRATEGY_REGISTRY defines HOW to compose indicators into strategies.
  - Adding a new indicator = add one _calc_{name} method + one registry entry.
  - Adding a new strategy = add one ta.Strategy + one _decide_{name} method.
    For multi-timeframe strategies, set multi_timeframe=True and add
    a _decide_mtf_{key} method that receives candles_by_tf directly.
  - get_display_config() returns the full rendering spec for the frontend.
  - compute_strategies() runs each ta.Strategy via df.ta.strategy() and
    produces an array of bull/bear/sideways decisions.
  - Frontend renders dynamically — no hardcoded indicator rows.

Workflow for adding a new indicator:
  1. Write _calc_{name}(self, df, **params) method
  2. Add entry to INDICATOR_REGISTRY with params, timeframes, and display slots
  3. (Optional) Add MIN_ROWS entry if pandas-ta needs more data than default
  4. Done — it appears in Redis, WS broadcast, and frontend automatically.

Workflow for adding a new strategy:
  1. Define a ta.Strategy(...) with the indicators it uses
  2. Add entry to STRATEGY_REGISTRY with timeframes and display config
  3. Add _decide_{key}(self, values, price) method for decision rules
  4. Wire it in _decide_strategy() dispatcher
  5. Done — strategy label appears in frontend automatically.

Redis Keys:
  indicators:{SYMBOL}  → Hash { "1m_rsi_14": "62.4", "1h_ema_9": "98900", ... }

Redis Pub/Sub (DB3):
  Channel: "indicators:{SYMBOL}"  → { values, strategies } on recalculation
"""

import json
import logging
from typing import Dict, List, Optional, Any

import pandas as pd
import pandas_ta_classic as ta

from .session_breakout import compute_session_breakout

logger = logging.getLogger("indicator.engine")

# Suppress pandas-ta verbose stdout warnings ([X] Series has N rows...)
# We handle insufficient data ourselves via MIN_ROWS check below.
try:
    ta.settings.verbose = False
except Exception:
    pass  # Older pandas-ta versions may not have settings

# ---------------------------------------------------------------------------
# Minimum data rows required per indicator type (computed from params)
# ---------------------------------------------------------------------------
# These are checked BEFORE calling pandas-ta, so it never prints warnings.
# Format: {indicator_name: lambda params -> int}
# ---------------------------------------------------------------------------
MIN_ROWS = {
    "rsi": lambda p: p.get("length", 14),
    "ema": lambda p: max(p.get("length", [9, 21])),
    "sma": lambda p: max(p.get("length", [20, 50])),
    "bbands": lambda p: p.get("length", 20),
    "macd": lambda p: p.get("slow", 26) + p.get("signal", 9),
    "atr": lambda p: p.get("length", 14),
    "stoch": lambda p: p.get("k", 14) + p.get("d", 3),
    "adx": lambda p: p.get("length", 14) * 2,
    "volume_sma": lambda p: p.get("length", 20),
}

FALLBACK_MIN_ROWS = 5  # For unregistered indicators

# ---------------------------------------------------------------------------
# Timeframe Display Configuration
# ---------------------------------------------------------------------------
DISPLAY_TIMEFRAMES = ["1m", "15m", "1h", "4h"]

TIMEFRAME_DISPLAY = {
    "1m": {"label": "1m", "color": "sky"},
    "15m": {"label": "15m", "color": "amber"},
    "1h": {"label": "1h", "color": "violet"},
    "4h": {"label": "4h", "color": "rose"},
}

# ---------------------------------------------------------------------------
# Indicator Registry — defines what to calculate + how to display it
# ---------------------------------------------------------------------------
# Each entry has:
#   params      — passed to _calc_{name}(df, **params)
#   timeframes  — which TFs to calculate on
#   order       — display order within a timeframe column (lower = top)
#   multi       — True if indicator produces multiple outputs
#
# For single-output indicators:
#   label, format, decimals, bold, font_size, color_rules, default_class
#
# For multi-output indicators:
#   slots — list of {suffix, label, format, decimals, bold, font_size,
#                     color_rules, default_class}
#   Each slot maps to one output key: {tf}_{name}_{suffix}
#
# Format types:
#   "decimal"  — toFixed(decimals)                          e.g. RSI 62.4
#   "price"    — $XX,XXX locale string                       e.g. EMA $98,900
#   "signed"   — +/- prefix + toFixed(decimals)             e.g. MACD +12.50
#   "volume"   — K/M suffixes                               e.g. VolSM 1.2M
#
# Color rules: [{"op": "gt"|"lt", "value": number, "class": "tailwind-class"}]
#   First matching rule wins. Falls back to default_class.
# ---------------------------------------------------------------------------
INDICATOR_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ── RSI ──────────────────────────────────────────────
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
    },

    # ── EMA (multi: 9, 21) ──────────────────────────────
    "ema": {
        "timeframes": ["1m", "15m", "1h", "4h"],
        "params": {"length": [9, 21]},
        "order": 1,
        "multi": True,
        "slots": [
            {"suffix": "9", "label": "EMA9", "format": "price", "decimals": 0,
             "font_size": "11px", "default_class": "text-gray-300 dark:text-gray-400"},
            {"suffix": "21", "label": "EMA21", "format": "price", "decimals": 0,
             "font_size": "11px", "default_class": "text-gray-300 dark:text-gray-400"},
        ],
    },

    # ── SMA (multi: 20, 50) ─────────────────────────────
    "sma": {
        "timeframes": ["1m", "15m", "1h"],
        "params": {"length": [20, 50]},
        "order": 2,
        "multi": True,
        "slots": [
            {"suffix": "20", "label": "SMA20", "format": "price", "decimals": 0,
             "font_size": "11px", "default_class": "text-gray-300 dark:text-gray-400"},
            {"suffix": "50", "label": "SMA50", "format": "price", "decimals": 0,
             "font_size": "11px", "default_class": "text-gray-300 dark:text-gray-400"},
        ],
    },

    # ── Bollinger Bands (multi: upper, mid, lower) ───────
    "bbands": {
        "timeframes": ["15m", "1h"],
        "params": {"length": 20, "std": 2},
        "order": 3,
        "multi": True,
        "slots": [
            {"suffix": "upper", "label": "BB-U", "format": "price", "decimals": 0,
             "font_size": "10px", "default_class": "text-gray-300 dark:text-gray-400"},
            {"suffix": "mid", "label": "BB-M", "format": "price", "decimals": 0,
             "font_size": "10px", "default_class": "text-gray-300 dark:text-gray-400"},
            {"suffix": "lower", "label": "BB-L", "format": "price", "decimals": 0,
             "font_size": "10px", "default_class": "text-gray-300 dark:text-gray-400"},
        ],
    },

    # ── ATR ─────────────────────────────────────────────
    "atr": {
        "timeframes": ["15m", "1h"],
        "params": {"length": 14},
        "order": 4,
        "label": "ATR",
        "format": "price",
        "decimals": 2,
        "font_size": "11px",
        "default_class": "text-gray-300 dark:text-gray-400",
    },

    # ── Stochastic (multi: k, d) ────────────────────────
    "stoch": {
        "timeframes": ["15m", "1h"],
        "params": {"k": 14, "d": 3, "smooth_k": 3},
        "order": 5,
        "multi": True,
        "slots": [
            {"suffix": "k", "label": "StoK", "format": "decimal", "decimals": 1,
             "font_size": "10px",
             "color_rules": [
                 {"op": "gt", "value": 80, "class": "text-red-400"},
                 {"op": "lt", "value": 20, "class": "text-emerald-400"},
             ],
             "default_class": "text-gray-300 dark:text-gray-400"},
            {"suffix": "d", "label": "StoD", "format": "decimal", "decimals": 1,
             "font_size": "10px",
             "color_rules": [
                 {"op": "gt", "value": 80, "class": "text-red-400"},
                 {"op": "lt", "value": 20, "class": "text-emerald-400"},
             ],
             "default_class": "text-gray-300 dark:text-gray-400"},
        ],
    },

    # ── ADX (multi: adx, dmp, dmn) ──────────────────────
    "adx": {
        "timeframes": ["1h", "4h"],
        "params": {"length": 14},
        "order": 6,
        "multi": True,
        "slots": [
            {"suffix": "adx", "label": "ADX", "format": "decimal", "decimals": 1,
             "font_size": "11px",
             "color_rules": [
                 {"op": "gt", "value": 25, "class": "text-amber-400"},
             ],
             "default_class": "text-gray-300 dark:text-gray-400"},
            {"suffix": "dmp", "label": "+DI", "format": "decimal", "decimals": 1,
             "font_size": "10px",
             "default_class": "text-emerald-400/70"},
            {"suffix": "dmn", "label": "-DI", "format": "decimal", "decimals": 1,
             "font_size": "10px",
             "default_class": "text-red-400/70"},
        ],
    },

    # ── MACD (multi: hist, line, signal) ────────────────
    "macd": {
        "timeframes": ["1h", "4h"],
        "params": {"fast": 12, "slow": 26, "signal": 9},
        "order": 7,
        "multi": True,
        "slots": [
            {"suffix": "hist", "label": "MACD", "format": "signed", "decimals": 2,
             "font_size": "10px",
             "color_rules": [
                 {"op": "gt", "value": 0, "class": "text-emerald-400"},
                 {"op": "lt", "value": 0, "class": "text-red-400"},
             ],
             "default_class": "text-gray-300 dark:text-gray-400"},
            {"suffix": "line", "label": "MACD-L", "format": "signed", "decimals": 2,
             "font_size": "10px",
             "default_class": "text-gray-300 dark:text-gray-400"},
            {"suffix": "signal", "label": "MACD-S", "format": "signed", "decimals": 2,
             "font_size": "10px",
             "default_class": "text-gray-300 dark:text-gray-400"},
        ],
    },

    # ── Volume SMA ──────────────────────────────────────
    "volume_sma": {
        "timeframes": ["1h"],
        "params": {"length": 20},
        "order": 8,
        "label": "VolSM",
        "format": "volume",
        "decimals": 0,
        "font_size": "10px",
        "default_class": "text-gray-300 dark:text-gray-400",
    },
}

# ---------------------------------------------------------------------------
# Strategy Definitions (pandas-ta Strategy Pattern)
# ---------------------------------------------------------------------------
# Each strategy is a ta.Strategy that composes indicators and runs via
# df.ta.strategy(). The engine extracts column values and passes them
# to a _decide_{key} method for bull/bear/sideways classification.
#
# Adding a new strategy:
#   1. Define ta.Strategy(...) with indicator list
#   2. Add entry to STRATEGY_REGISTRY below
#   3. Add _decide_{key}() method on IndicatorEngine
#   4. Wire it in _decide_strategy() dispatcher
#   5. Done — appears on all asset cards automatically.
# ---------------------------------------------------------------------------

MomentumStrategy = ta.Strategy(
    name="Momentum",
    description="Short-term momentum: RSI + MACD + Stochastic",
    ta=[
        {"kind": "rsi", "length": 14},
        {"kind": "macd", "fast": 12, "slow": 26, "signal": 9},
        {"kind": "stoch", "k": 14, "d": 3, "smooth_k": 3},
    ],
)

TrendStrategy = ta.Strategy(
    name="Trend",
    description="Trend direction and strength: EMA stack + SMA50 + ADX",
    ta=[
        {"kind": "ema", "length": 9},
        {"kind": "ema", "length": 21},
        {"kind": "sma", "length": 50},
        {"kind": "adx", "length": 14},
    ],
)

VolatilityStrategy = ta.Strategy(
    name="Volatility",
    description="Mean reversion: Bollinger Bands position + ATR",
    ta=[
        {"kind": "bbands", "length": 20},
        {"kind": "atr", "length": 14},
    ],
)

STRATEGY_REGISTRY: List[Dict[str, Any]] = [
    {
        "key": "momentum",
        "strategy": MomentumStrategy,
        "timeframes": ["1h", "15m"],  # Try 1h first, fallback to 15m
        "display": {"label": "Mom", "order": 0},
    },
    {
        "key": "trend",
        "strategy": TrendStrategy,
        "timeframes": ["4h", "1h"],  # Try 4h first, fallback to 1h
        "display": {"label": "Trend", "order": 1},
    },
    {
        "key": "volatility",
        "strategy": VolatilityStrategy,
        "timeframes": ["1h", "15m"],
        "display": {"label": "Vol", "order": 2},
    },
    # ── Multi-Timeframe Strategy ────────────────────────────────────
    # Fast Short-Trend: EMA13/WMA44 crossover across 3 timeframes.
    # Original concept: 15m → 1h → 1d.  Scaled to available data:
    #   Primary  = 5m  (EMA13 × WMA44 crossover + near/crossing detection)
    #   Mid      = 15m (EMA13_5m vs WMA44_15m alignment)
    #   High     = 4h  (EMA13_5m vs WMA44_4h alignment)
    #   Price filter: price > WMA44 for last 2-3 candles on 15m and 4h.
    #
    # Ratios preserved:  5m:15m = 1:3  (orig 15m:1h = 1:4)
    #                    5m:4h  = 1:48 (orig 15m:1d = 1:96)
    #                    15m:4h = 1:16 (orig 1h:1d  = 1:24)
    #
    # multi_timeframe=True → engine passes candles_by_tf directly
    # instead of running df.ta.strategy() on a single TF.
    {
        "key": "fast_trend",
        "timeframes": ["5m", "15m", "4h"],
        "display": {"label": "FST", "order": 3},
        "multi_timeframe": True,
    },
    # ── Session Breakout Strategy (SBS) ──────────────────────────────
    # Opening Range Breakout within market sessions using Initial Balance.
    #   IB Period:      30 min from session start
    #   Breakout Window: IB end + 90 min (total 2h from session open)
    #   Entry TF:        1m/5m candles, RSI(14) on 5m
    #   Filters:         ATR range check, CME gap, body ≥ 0.5
    #   After breakout:  TP1 = 1× IB range, TP2 = 2× IB range, SL = 0.1× range
    #   Post-window:     FADE mode (contrarian) if no breakout
    #   No session:      label "—"
    #
    # Uses session_breakout.py for all computation (pure function, no Redis).
    # Returns sbs_data with IB levels, progressive mid, CME gap, TP/SL.
    {
        "key": "session_breakout",
        "timeframes": ["1m", "5m", "1h"],
        "display": {"label": "SBS", "order": 4},
        "multi_timeframe": True,
    },
]


class IndicatorEngine:
    """
    Modular technical indicator engine.
    Calculates indicators from OHLCV candles using pandas-ta.
    """

    def __init__(self):
        self.registry = INDICATOR_REGISTRY.copy()

    # ------------------------------------------------------------------
    # Display Configuration — consumed by frontend for dynamic rendering
    # ------------------------------------------------------------------

    @staticmethod
    def get_display_config() -> dict:
        """
        Generate the full indicator display configuration for the frontend.
        Returns a JSON-serializable dict that the frontend uses to
        dynamically render all indicator rows — no hardcoded HTML needed.

        Response structure:
        {
            "timeframes": [
                {"key": "1m", "label": "1m", "color": "sky"},
                ...
            ],
            "grid_columns": 4,
            "indicators": {
                "1m": [
                    {
                        "key": "1m_rsi_14",
                        "label": "RSI",
                        "format": "decimal",
                        "decimals": 1,
                        "bold": true,
                        "font_size": "12px",
                        "color_rules": [...],
                        "default_class": "..."
                    },
                    ...
                ],
                "15m": [...],
                "1h": [...],
                "4h": [...]
            }
        }
        """
        config = {
            "timeframes": [],
            "grid_columns": len(DISPLAY_TIMEFRAMES),
            "indicators": {},
        }

        for tf_key in DISPLAY_TIMEFRAMES:
            tf_display = TIMEFRAME_DISPLAY.get(tf_key, {})
            config["timeframes"].append({
                "key": tf_key,
                "label": tf_display.get("label", tf_key),
                "color": tf_display.get("color", "gray"),
            })

            # Collect all indicator slots for this timeframe, sorted by order
            slots = []
            for ind_name, ind_cfg in INDICATOR_REGISTRY.items():
                if tf_key not in ind_cfg.get("timeframes", []):
                    continue

                if ind_cfg.get("multi"):
                    # Multi-output: iterate slots
                    for slot in ind_cfg.get("slots", []):
                        slot_entry = {
                            "key": f"{tf_key}_{ind_name}_{slot['suffix']}",
                            "label": slot["label"],
                            "format": slot["format"],
                            "decimals": slot["decimals"],
                            "bold": slot.get("bold", False),
                            "font_size": slot["font_size"],
                            "color_rules": slot.get("color_rules", []),
                            "default_class": slot["default_class"],
                        }
                        slots.append((ind_cfg.get("order", 99), slot_entry))
                else:
                    # Single-output
                    slot_entry = {
                        "key": f"{tf_key}_{ind_name}_{_param_str(ind_cfg['params'])}",
                        "label": ind_cfg["label"],
                        "format": ind_cfg["format"],
                        "decimals": ind_cfg["decimals"],
                        "bold": ind_cfg.get("bold", False),
                        "font_size": ind_cfg["font_size"],
                        "color_rules": ind_cfg.get("color_rules", []),
                        "default_class": ind_cfg["default_class"],
                    }
                    slots.append((ind_cfg.get("order", 99), slot_entry))

            # Sort by order (stable — preserves insertion order for ties)
            slots.sort(key=lambda x: x[0])
            config["indicators"][tf_key] = [s[1] for s in slots]

        return config

    # ------------------------------------------------------------------
    # Strategy Engine — multi-strategy decisions using ta.Strategy
    # ------------------------------------------------------------------

    def compute_strategies(
        self, candles_by_tf: Dict[str, List[Dict]], price: float = None
    ) -> list:
        """
        Run each registered strategy using df.ta.strategy() and return decisions.

        Uses pandas-ta Strategy pattern for clean indicator composition.
        Each strategy runs on its preferred timeframe, extracts indicator
        values from the DataFrame, and applies strategy-specific rules.

        Returns:
            [
                {
                    "key": "momentum",
                    "name": "Momentum",
                    "label": "BULLISH",
                    "color": "emerald",
                    "bull_score": 3,
                    "bear_score": 0,
                    "rules_matched": ["RSI_oversold", "MACD_bull", "Stoch_oversold"],
                    "display": {"label": "Mom", "order": 0},
                    "timeframe": "1h",
                },
                ...
            ]
        """
        results = []

        for strat_cfg in STRATEGY_REGISTRY:
            key = strat_cfg["key"]
            tfs = strat_cfg["timeframes"]
            display = strat_cfg["display"]
            is_mtf = strat_cfg.get("multi_timeframe", False)

            # ── Multi-timeframe strategy: pass full candles_by_tf ──
            if is_mtf:
                try:
                    decision = self._decide_strategy_mtf(
                        key, candles_by_tf, price=price
                    )
                except Exception as e:
                    logger.debug(f"Multi-TF strategy '{key}' failed: {e}")
                    decision = {
                        "label": "—", "color": "gray",
                        "bull_score": 0, "bear_score": 0, "rules_matched": [],
                    }
                # Extract sbs_data if present (Session Breakout Strategy)
                sbs_data = decision.pop("sbs_data", None)

                entry = {
                    "key": key,
                    "name": f"{display.get('label', key)} (Short)",
                    "label": decision["label"],
                    "color": decision["color"],
                    "bull_score": decision["bull_score"],
                    "bear_score": decision["bear_score"],
                    "rules_matched": decision.get("rules_matched", []),
                    "display": display,
                    "timeframe": "+".join(tfs),
                }
                if sbs_data:
                    entry["sbs_data"] = sbs_data

                results.append(entry)
                continue

            # ── Single-timeframe strategy (standard df.ta.strategy) ──
            strategy = strat_cfg["strategy"]
            extracted = None
            used_tf = None
            for tf in tfs:
                candles = candles_by_tf.get(tf, [])
                if len(candles) < 50:
                    continue
                df = self._candles_to_dataframe(candles)
                if df is None or len(df) < 50:
                    continue

                try:
                    df_copy = df.copy()
                    df_copy.ta.cores = 0  # No multiprocessing in Celery worker
                    df_copy.ta.strategy(strategy)

                    # Extract last row values (skip OHLCV columns)
                    last = df_copy.iloc[-1]
                    extracted = {}
                    for col in df_copy.columns:
                        if col in ("open", "high", "low", "close", "volume"):
                            continue
                        val = last[col]
                        if val is not None and not (isinstance(val, float) and val != val):
                            extracted[col] = float(val)

                    if extracted:
                        used_tf = tf
                        break
                except Exception as e:
                    logger.debug(f"Strategy '{key}' failed on {tf}: {e}")
                    continue

            # Apply strategy-specific decision rules
            if extracted:
                decision = self._decide_strategy(key, extracted, price=price)
            else:
                decision = {
                    "label": "—", "color": "gray",
                    "bull_score": 0, "bear_score": 0, "rules_matched": [],
                }

            results.append({
                "key": key,
                "name": strategy.name,
                "label": decision["label"],
                "color": decision["color"],
                "bull_score": decision["bull_score"],
                "bear_score": decision["bear_score"],
                "rules_matched": decision.get("rules_matched", []),
                "display": display,
                "timeframe": used_tf,
            })

        # Sort by display order
        results.sort(key=lambda x: x.get("display", {}).get("order", 99))
        return results

    def _decide_strategy(self, key: str, values: dict, price: float = None) -> dict:
        """Route to the appropriate decision function for this strategy."""
        decider = getattr(self, f"_decide_{key}", None)
        if decider:
            return decider(values, price=price)
        return {
            "label": "NEUTRAL", "color": "gray",
            "bull_score": 0, "bear_score": 0, "rules_matched": [],
        }

    def _decide_momentum(self, values: dict, price: float = None) -> dict:
        """
        Momentum Strategy Decision.
        Uses RSI, MACD histogram, and Stochastic K across one timeframe.

        Rules:
          RSI < 30 = bullish (oversold),  RSI > 70 = bearish (overbought)
          MACD hist > 0 = bullish,          MACD hist < 0 = bearish
          Stoch K < 20 = bullish (oversold), Stoch K > 80 = bearish (overbought)
        """
        bull, bear, matched = 0, 0, []

        # RSI
        rsi = _find_value(values, ["RSI_"])
        if rsi is not None:
            if rsi < 30:
                bull += 1
                matched.append("RSI_oversold")
            elif rsi > 70:
                bear += 1
                matched.append("RSI_overbought")

        # MACD histogram
        macd_hist = _find_value(values, ["MACDh_"])
        if macd_hist is not None:
            if macd_hist > 0:
                bull += 1
                matched.append("MACD_bull")
            elif macd_hist < 0:
                bear += 1
                matched.append("MACD_bear")

        # Stochastic K
        stoch_k = _find_value(values, ["STOCHk"])
        if stoch_k is not None:
            if stoch_k < 20:
                bull += 1
                matched.append("Stoch_oversold")
            elif stoch_k > 80:
                bear += 1
                matched.append("Stoch_overbought")

        label, color = _classify_decision(bull, bear)
        return {
            "label": label, "color": color,
            "bull_score": bull, "bear_score": bear, "rules_matched": matched,
        }

    def _decide_trend(self, values: dict, price: float = None) -> dict:
        """
        Trend Strategy Decision.
        Uses EMA9/EMA21 stack, SMA50 position, and ADX strength.

        Rules:
          Price > EMA9 > EMA21 = strong bullish (2 pts)
          Price < EMA9 < EMA21 = strong bearish (2 pts)
          EMA9 > EMA21 (no price) = mild bull/bear (1 pt)
          Price > SMA50 = bullish,  Price < SMA50 = bearish
          ADX < 20 = informational (sideways) — doesn't score
        """
        bull, bear, matched = 0, 0, []

        ema9 = _find_value(values, ["EMA_9"])
        ema21 = _find_value(values, ["EMA_21"])
        sma50 = _find_value(values, ["SMA_50"])
        adx = _find_value(values, ["ADX_"])

        # EMA stack with price
        if price is not None and ema9 is not None and ema21 is not None:
            if price > ema9 > ema21:
                bull += 2
                matched.append("Price>EMA9>EMA21")
            elif price < ema9 < ema21:
                bear += 2
                matched.append("Price<EMA9<EMA21")
        elif ema9 is not None and ema21 is not None:
            if ema9 > ema21:
                bull += 1
                matched.append("EMA9>EMA21")
            else:
                bear += 1
                matched.append("EMA9<EMA21")

        # SMA50 position
        if price is not None and sma50 is not None:
            if price > sma50:
                bull += 1
                matched.append("Price>SMA50")
            else:
                bear += 1
                matched.append("Price<SMA50")

        # ADX strength (informational — doesn't score but adds context)
        if adx is not None:
            if adx < 20:
                matched.append("ADX_weak_trend")
            elif adx > 25:
                matched.append("ADX_strong_trend")

        label, color = _classify_decision(bull, bear)
        return {
            "label": label, "color": color,
            "bull_score": bull, "bear_score": bear, "rules_matched": matched,
        }

    def _decide_volatility(self, values: dict, price: float = None) -> dict:
        """
        Volatility / Mean Reversion Strategy Decision.
        Uses Bollinger Band position relative to price.

        Rules:
          Price ≤ BB lower = strongly bullish (oversold, 2 pts)
          Price ≥ BB upper = strongly bearish (overbought, 2 pts)
          Price < BB mid = mild bullish (1 pt)
          Price > BB mid = mild bearish (1 pt)
        """
        bull, bear, matched = 0, 0, []

        bb_lower = _find_value(values, ["BBL_"])
        bb_upper = _find_value(values, ["BBU_"])
        bb_mid = _find_value(values, ["BBM_"])

        if price is not None and bb_lower is not None and bb_upper is not None:
            if price <= bb_lower:
                bull += 2
                matched.append("BB_oversold")
            elif price >= bb_upper:
                bear += 2
                matched.append("BB_overbought")
            elif bb_mid is not None:
                if price < bb_mid:
                    bull += 1
                    matched.append("BB_below_mid")
                else:
                    bear += 1
                    matched.append("BB_above_mid")

        label, color = _classify_decision(bull, bear)
        return {
            "label": label, "color": color,
            "bull_score": bull, "bear_score": bear, "rules_matched": matched,
        }

    # ------------------------------------------------------------------
    # Multi-Timeframe Strategy Dispatcher
    # ------------------------------------------------------------------

    def _decide_strategy_mtf(
        self, key: str, candles_by_tf: Dict[str, List[Dict]], price: float = None
    ) -> dict:
        """
        Route multi-timeframe strategies to their handler.
        Receives the full candles_by_tf dict so the handler can
        compute indicators across multiple timeframes.
        """
        decider = getattr(self, f"_decide_mtf_{key}", None)
        if decider:
            return decider(candles_by_tf, price=price)
        return {
            "label": "NEUTRAL", "color": "gray",
            "bull_score": 0, "bear_score": 0, "rules_matched": [],
        }

    def _decide_mtf_fast_trend(
        self, candles_by_tf: Dict[str, List[Dict]], price: float = None
    ) -> dict:
        """
        Fast Short-Trend (FST): Multi-timeframe EMA13/WMA44 strategy.

        Logic (scaled from original 15m→1h→1d concept):
          ┌─────────────────────────────────────────────────────────────┐
          │ PRIMARY (5m):  EMA13(5m) vs WMA44(5m)                     │
          │   - Must be very near / crossing up   → +2 bull            │
          │   - Must be > (not near, already wide)  → +1 bull           │
          │   - Opposite                           → bear scores        │
          │                                                                 │
          │ MID (15m):     EMA13(5m) vs WMA44(15m)                     │
          │   - EMA13(5m) > WMA44(15m)            → +1 bull            │
          │   - Opposite                           → +1 bear            │
          │                                                                 │
          │ HIGH (4h):      EMA13(5m) vs WMA44(4h)                      │
          │   - EMA13(5m) > WMA44(4h)             → +1 bull            │
          │   - Opposite                           → +1 bear            │
          │                                                                 │
          │ PRICE FILTER (15m + 4h):                                        │
          │   - Price > WMA44 on last 2-3 candles  → +1 bull per TF     │
          │   - Price < WMA44 on last 2-3 candles  → +1 bear per TF     │
          └─────────────────────────────────────────────────────────────┘

        Scoring:
          ≥4 bull, bear ≤ 1  → BULLISH
          ≥3 bull, bear ≤ 0  → LEAN BULL
          ≥4 bear, bull ≤ 1  → BEARISH
          ≥3 bear, bull ≤ 0  → LEAN BEAR
          else               → NEUTRAL
        """
        bull, bear, matched = 0, 0, []

        # ── Helper: compute EMA and WMA series from candles ──
        def _calc_ema_series(candles, length):
            if len(candles) < length:
                return None
            df = self._candles_to_dataframe(candles)
            if df is None:
                return None
            try:
                return ta.ema(df["close"], length=length)
            except Exception:
                return None

        def _calc_wma_series(candles, length):
            if len(candles) < length:
                return None
            df = self._candles_to_dataframe(candles)
            if df is None:
                return None
            try:
                return ta.wma(df["close"], length=length)
            except Exception:
                return None

        def _get_close_series(candles):
            if len(candles) < 3:
                return None
            df = self._candles_to_dataframe(candles)
            if df is None:
                return None
            return df["close"]

        # ── 1) PRIMARY (5m): EMA13 vs WMA44 crossover ──
        candles_5m = candles_by_tf.get("5m", [])
        ema13_5m = _calc_ema_series(candles_5m, 13)
        wma44_5m = _calc_wma_series(candles_5m, 44)

        if ema13_5m is not None and wma44_5m is not None:
            # Drop NaN to get valid index range
            valid = ema13_5m.notna() & wma44_5m.notna()
            if valid.sum() >= 2:
                cur_ema = float(ema13_5m[valid].iloc[-1])
                cur_wma = float(wma44_5m[valid].iloc[-1])
                prev_ema = float(ema13_5m[valid].iloc[-2])
                prev_wma = float(wma44_5m[valid].iloc[-2])

                # "Very near" = within 0.3% of each other
                gap_pct = abs(cur_ema - cur_wma) / cur_wma * 100 if cur_wma != 0 else 999
                is_near = gap_pct < 0.3

                # Crossing up: EMA was below or near WMA, now above
                crossing_up = (
                    prev_ema <= prev_wma and cur_ema > cur_wma
                ) or (
                    is_near and prev_ema < cur_ema > cur_wma
                )
                # Crossing down (bearish mirror)
                crossing_down = (
                    prev_ema >= prev_wma and cur_ema < cur_wma
                ) or (
                    is_near and prev_ema > cur_ema < cur_wma
                )

                if crossing_up:
                    bull += 2
                    matched.append("5m_cross_up")
                elif crossing_down:
                    bear += 2
                    matched.append("5m_cross_down")
                elif cur_ema > cur_wma:
                    bull += 1
                    matched.append("5m_EMA>WMA")
                elif cur_ema < cur_wma:
                    bear += 1
                    matched.append("5m_EMA<WMA")
                else:
                    matched.append("5m_flat")

        # ── 2) MID (15m): EMA13(5m) vs WMA44(15m) alignment ──
        candles_15m = candles_by_tf.get("15m", [])
        wma44_15m = _calc_wma_series(candles_15m, 44)

        if ema13_5m is not None and wma44_15m is not None:
            valid_ema = ema13_5m.notna()
            valid_wma = wma44_15m.notna()
            if valid_ema.sum() >= 1 and valid_wma.sum() >= 1:
                cur_ema_5m = float(ema13_5m[valid_ema].iloc[-1])
                cur_wma_15m = float(wma44_15m[valid_wma].iloc[-1])

                if cur_ema_5m > cur_wma_15m:
                    bull += 1
                    matched.append("15m_aligned")
                elif cur_ema_5m < cur_wma_15m:
                    bear += 1
                    matched.append("15m_misaligned")

        # ── 3) HIGH (4h): EMA13(5m) vs WMA44(4h) alignment ──
        candles_4h = candles_by_tf.get("4h", [])
        wma44_4h = _calc_wma_series(candles_4h, 44)

        if ema13_5m is not None and wma44_4h is not None:
            valid_ema = ema13_5m.notna()
            valid_wma = wma44_4h.notna()
            if valid_ema.sum() >= 1 and valid_wma.sum() >= 1:
                cur_ema_5m = float(ema13_5m[valid_ema].iloc[-1])
                cur_wma_4h = float(wma44_4h[valid_wma].iloc[-1])

                if cur_ema_5m > cur_wma_4h:
                    bull += 1
                    matched.append("4h_aligned")
                elif cur_ema_5m < cur_wma_4h:
                    bear += 1
                    matched.append("4h_misaligned")

        # ── 4) PRICE FILTER: price vs WMA44 on 15m and 4h ──
        lookback = 3  # check last 3 candles

        # 15m price filter
        close_15m = _get_close_series(candles_15m)
        if close_15m is not None and wma44_15m is not None:
            valid_c = close_15m.notna()
            valid_w = wma44_15m.notna()
            overlap = valid_c & valid_w
            if overlap.sum() >= lookback:
                above_count = 0
                below_count = 0
                for i in range(-lookback, 0):
                    c = float(close_15m[overlap].iloc[i])
                    w = float(wma44_15m[overlap].iloc[i])
                    if c > w:
                        above_count += 1
                    elif c < w:
                        below_count += 1
                # Require majority (2 out of 3)
                if above_count >= 2:
                    bull += 1
                    matched.append("15m_price_above")
                elif below_count >= 2:
                    bear += 1
                    matched.append("15m_price_below")

        # 4h price filter
        close_4h = _get_close_series(candles_4h)
        if close_4h is not None and wma44_4h is not None:
            valid_c = close_4h.notna()
            valid_w = wma44_4h.notna()
            overlap = valid_c & valid_w
            if overlap.sum() >= lookback:
                above_count = 0
                below_count = 0
                for i in range(-lookback, 0):
                    c = float(close_4h[overlap].iloc[i])
                    w = float(wma44_4h[overlap].iloc[i])
                    if c > w:
                        above_count += 1
                    elif c < w:
                        below_count += 1
                if above_count >= 2:
                    bull += 1
                    matched.append("4h_price_above")
                elif below_count >= 2:
                    bear += 1
                    matched.append("4h_price_below")

        # ── 5) Classify ──
        # Multi-TF has more components — use slightly higher thresholds
        if bull >= 4 and bear <= 1:
            label, color = "BULLISH", "emerald"
        elif bear >= 4 and bull <= 1:
            label, color = "BEARISH", "red"
        elif bull >= 3 and bear <= 0:
            label, color = "LEAN BULL", "emerald"
        elif bear >= 3 and bull <= 0:
            label, color = "LEAN BEAR", "red"
        elif bull > bear:
            label, color = "LEAN BULL", "emerald"
        elif bear > bull:
            label, color = "LEAN BEAR", "red"
        else:
            label, color = "NEUTRAL", "gray"

        return {
            "label": label, "color": color,
            "bull_score": bull, "bear_score": bear, "rules_matched": matched,
        }

    def _decide_mtf_session_breakout(
        self, candles_by_tf: Dict[str, List[Dict]], price: float = None
    ) -> dict:
        """
        Session Breakout Strategy (SBS): Opening Range Breakout with IB.

        Delegates to the standalone compute_session_breakout() function
        in session_breakout.py. This wrapper just adapts the interface.

        The function returns the standard decision dict PLUS sbs_data
        with IB levels, progressive mid, CME gap, and TP/SL for frontend.

        States:
          "IB"       → Initial Balance forming (0–30 min)
          "BULLISH"  → Long breakout confirmed (close > IB_H, RSI>55, etc.)
          "BEARISH"  → Short breakout confirmed (close < IB_L, RSI<45, etc.)
          "LEAN BULL/BEAR" → Price leaning toward breakout (30–120 min)
          "FADE"     → Post-window, no breakout → contrarian mode
          "—"        → No active session
        """
        if price is None:
            price = 0.0

        try:
            result = compute_session_breakout(candles_by_tf, price)
        except Exception as e:
            logger.debug(f"SBS strategy failed: {e}")
            result = {
                "label": "—", "color": "gray",
                "bull_score": 0, "bear_score": 0, "rules_matched": [],
                "sbs_data": {},
            }

        return result

    # ------------------------------------------------------------------
    # Public API: Calculation
    # ------------------------------------------------------------------

    def calculate_all(
        self, candles_by_tf: Dict[str, List[Dict]]
    ) -> Dict[str, float]:
        """
        Calculate all registered indicators for all timeframes.
        Returns flat key-value pairs: { "1m_rsi_14": 62.4, "1h_ema_9": 98900, ... }
        """
        results = {}

        for indicator_name, config in self.registry.items():
            handler = getattr(self, f"_calc_{indicator_name}", None)
            if not handler:
                logger.warning(f"No handler for indicator '{indicator_name}', skipping")
                continue

            for tf in config.get("timeframes", []):
                candles = candles_by_tf.get(tf, [])
                if len(candles) < FALLBACK_MIN_ROWS:
                    continue

                try:
                    df = self._candles_to_dataframe(candles)
                    if df is None or len(df) < FALLBACK_MIN_ROWS:
                        continue

                    # --- Minimum data validation: skip BEFORE calling pandas-ta ---
                    params = config.get("params", {})
                    min_fn = MIN_ROWS.get(indicator_name)
                    required = min_fn(params) if min_fn else FALLBACK_MIN_ROWS
                    if len(df) < required:
                        logger.debug(
                            f"Skipping {indicator_name} on {tf}: "
                            f"{len(df)} rows < {required} required"
                        )
                        continue

                    if config.get("multi"):
                        # Multi-output indicator — returns dict of {suffix: value}
                        multi_results = handler(df, **params)
                        if multi_results:
                            for suffix, value in multi_results.items():
                                key = f"{tf}_{indicator_name}_{suffix}"
                                results[key] = round(float(value), 6) if value is not None else None
                    else:
                        # Single-output indicator
                        value = handler(df, **params)
                        if value is not None:
                            param_str = _param_str(params)
                            key = f"{tf}_{indicator_name}_{param_str}"
                            results[key] = round(float(value), 6)

                except Exception as e:
                    logger.debug(
                        f"Indicator {indicator_name} failed on {tf}: {e}"
                    )
                    continue

        return results

    def calculate_all_with_strategy(
        self, candles_by_tf: Dict[str, List[Dict]], price: float = None
    ) -> dict:
        """
        Calculate indicators + multi-strategy decisions.
        Used for WS broadcast — frontend consumes this directly.

        Returns:
            {
                "values": { "1m_rsi_14": 62.4, ... },            # indicator grid
                "strategies": [{ "key": "momentum", ... }, ...],   # strategy decisions
            }
        """
        values = self.calculate_all(candles_by_tf)
        strategies = self.compute_strategies(candles_by_tf, price=price)
        return {
            "values": values,
            "strategies": strategies,
        }

    # ------------------------------------------------------------------
    # Indicator Implementations (private, named _calc_{name})
    # ------------------------------------------------------------------

    def _calc_rsi(self, df: pd.DataFrame, length: int = 14) -> Optional[float]:
        """Relative Strength Index."""
        result = ta.rsi(df["close"], length=length)
        if result is not None and len(result) > 0:
            return float(result.iloc[-1])
        return None

    def _calc_ema(
        self, df: pd.DataFrame, length: list = None
    ) -> Dict[str, float]:
        """Exponential Moving Average — multi-output."""
        if not length:
            length = [9, 21]
        results = {}
        for period in length:
            result = ta.ema(df["close"], length=period)
            if result is not None and len(result) > 0:
                val = float(result.iloc[-1])
                if val is not None:
                    results[str(period)] = val
        return results

    def _calc_sma(
        self, df: pd.DataFrame, length: list = None
    ) -> Dict[str, float]:
        """Simple Moving Average — multi-output."""
        if not length:
            length = [20, 50]
        results = {}
        for period in length:
            result = ta.sma(df["close"], length=period)
            if result is not None and len(result) > 0:
                val = float(result.iloc[-1])
                if val is not None:
                    results[str(period)] = val
        return results

    def _calc_bbands(
        self, df: pd.DataFrame, length: int = 20, std: int = 2
    ) -> Dict[str, float]:
        """Bollinger Bands — returns upper, mid, lower."""
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

    def _calc_macd(
        self, df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Dict[str, float]:
        """MACD — returns macd_line, signal_line, histogram."""
        result = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
        if result is not None and len(result) > 0:
            row = result.iloc[-1]
            results = {}
            for col in result.columns:
                if col.startswith("MACD_") and "signal" not in col and "hist" not in col:
                    results["line"] = float(row[col])
                elif "signal" in col:
                    results["signal"] = float(row[col])
                elif "hist" in col:
                    results["hist"] = float(row[col])
            return results if results else {}
        return {}

    def _calc_atr(self, df: pd.DataFrame, length: int = 14) -> Optional[float]:
        """Average True Range."""
        result = ta.atr(df["high"], df["low"], df["close"], length=length)
        if result is not None and len(result) > 0:
            return float(result.iloc[-1])
        return None

    def _calc_stoch(
        self, df: pd.DataFrame, k: int = 14, d: int = 3, smooth_k: int = 3
    ) -> Dict[str, float]:
        """Stochastic Oscillator — returns k and d values."""
        result = ta.stoch(
            df["high"], df["low"], df["close"],
            k=k, d=d, smooth_k=smooth_k
        )
        if result is not None and len(result) > 0:
            row = result.iloc[-1]
            results = {}
            for col in result.columns:
                if col.startswith("STOCHk"):
                    results["k"] = float(row[col])
                elif col.startswith("STOCHd"):
                    results["d"] = float(row[col])
            return results if results else {}
        return {}

    def _calc_adx(self, df: pd.DataFrame, length: int = 14) -> Dict[str, float]:
        """Average Directional Index — returns adx, dmp (+DI), dmn (-DI)."""
        result = ta.adx(df["high"], df["low"], df["close"], length=length)
        if result is not None and len(result) > 0:
            row = result.iloc[-1]
            results = {}
            for col in result.columns:
                if col.startswith("ADX_"):
                    results["adx"] = float(row[col])
                elif col.startswith("DMP_"):
                    results["dmp"] = float(row[col])
                elif col.startswith("DMN_"):
                    results["dmn"] = float(row[col])
            return results if results else {}
        return {}

    def _calc_volume_sma(
        self, df: pd.DataFrame, length: int = 20
    ) -> Optional[float]:
        """Volume Simple Moving Average."""
        result = ta.sma(df["volume"], length=length)
        if result is not None and len(result) > 0:
            return float(result.iloc[-1])
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _candles_to_dataframe(candles: List[Dict]) -> Optional[pd.DataFrame]:
        """
        Convert list of candle dicts to a pandas DataFrame
        with columns: open, high, low, close, volume.
        """
        if not candles:
            return None

        try:
            df = pd.DataFrame(candles)
            df = df.rename(columns={
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
            })
            # Ensure numeric types
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = pd.to_numeric(df[col], errors="coerce")

            # Drop rows with NaN OHLCV
            df = df.dropna(subset=["open", "high", "low", "close"])

            if len(df) == 0:
                return None

            return df[["open", "high", "low", "close", "volume"]]

        except Exception as e:
            logger.warning(f"Failed to convert candles to DataFrame: {e}")
            return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _param_str(params: dict) -> str:
    """Convert params dict to underscore-joined string for key generation."""
    return "_".join(str(v) for v in params.values())


def _find_value(values: dict, prefixes: list) -> Optional[float]:
    """
    Find the first value whose key matches any of the given prefixes.
    Used to extract pandas-ta column values without hardcoding exact column names.
    Example: _find_value(values, ["MACDh_"]) → finds MACDh_12_26_9
    """
    for col, val in values.items():
        for prefix in prefixes:
            if col.startswith(prefix):
                return val
    return None


def _classify_decision(bull: int, bear: int) -> tuple:
    """Map bull/bear point scores to a (label, color) decision tuple."""
    if bull >= 2 and bull > bear + 1:
        return "BULLISH", "emerald"
    elif bear >= 2 and bear > bull + 1:
        return "BEARISH", "red"
    elif bull >= 1 and bear <= 0:
        return "LEAN BULL", "emerald"
    elif bear >= 1 and bull <= 0:
        return "LEAN BEAR", "red"
    else:
        return "NEUTRAL", "gray"



# """
# Indicator Engine — Modular Technical Analysis
# ==============================================
# Calculates technical indicators from OHLCV candle data using pandas-ta.
# Completely independent from the 7-variable intelligence engine.

# Architecture:
#   - INDICATOR_REGISTRY defines WHAT to calculate and HOW to display it.
#   - STRATEGY_REGISTRY defines HOW to compose indicators into strategies.
#   - Adding a new indicator = add one _calc_{name} method + one registry entry.
#   - Adding a new strategy = add one ta.Strategy + one _decide_{name} method.
#     For multi-timeframe strategies, set multi_timeframe=True and add
#     a _decide_mtf_{key} method that receives candles_by_tf directly.
#   - get_display_config() returns the full rendering spec for the frontend.
#   - compute_strategies() runs each ta.Strategy via df.ta.strategy() and
#     produces an array of bull/bear/sideways decisions.
#   - Frontend renders dynamically — no hardcoded indicator rows.

# Workflow for adding a new indicator:
#   1. Write _calc_{name}(self, df, **params) method
#   2. Add entry to INDICATOR_REGISTRY with params, timeframes, and display slots
#   3. (Optional) Add MIN_ROWS entry if pandas-ta needs more data than default
#   4. Done — it appears in Redis, WS broadcast, and frontend automatically.

# Workflow for adding a new strategy:
#   1. Define a ta.Strategy(...) with the indicators it uses
#   2. Add entry to STRATEGY_REGISTRY with timeframes and display config
#   3. Add _decide_{key}(self, values, price) method for decision rules
#   4. Wire it in _decide_strategy() dispatcher
#   5. Done — strategy label appears in frontend automatically.

# Redis Keys:
#   indicators:{SYMBOL}  → Hash { "1m_rsi_14": "62.4", "1h_ema_9": "98900", ... }

# Redis Pub/Sub (DB3):
#   Channel: "indicators:{SYMBOL}"  → { values, strategies } on recalculation
# """

# import json
# import logging
# from typing import Dict, List, Optional, Any

# import pandas as pd
# import pandas_ta_classic as ta

# logger = logging.getLogger("indicator.engine")

# # Suppress pandas-ta verbose stdout warnings ([X] Series has N rows...)
# # We handle insufficient data ourselves via MIN_ROWS check below.
# try:
#     ta.settings.verbose = False
# except Exception:
#     pass  # Older pandas-ta versions may not have settings

# # ---------------------------------------------------------------------------
# # Minimum data rows required per indicator type (computed from params)
# # ---------------------------------------------------------------------------
# # These are checked BEFORE calling pandas-ta, so it never prints warnings.
# # Format: {indicator_name: lambda params -> int}
# # ---------------------------------------------------------------------------
# MIN_ROWS = {
#     "rsi": lambda p: p.get("length", 14),
#     "ema": lambda p: max(p.get("length", [9, 21])),
#     "sma": lambda p: max(p.get("length", [20, 50])),
#     "bbands": lambda p: p.get("length", 20),
#     "macd": lambda p: p.get("slow", 26) + p.get("signal", 9),
#     "atr": lambda p: p.get("length", 14),
#     "stoch": lambda p: p.get("k", 14) + p.get("d", 3),
#     "adx": lambda p: p.get("length", 14) * 2,
#     "volume_sma": lambda p: p.get("length", 20),
# }

# FALLBACK_MIN_ROWS = 5  # For unregistered indicators

# # ---------------------------------------------------------------------------
# # Timeframe Display Configuration
# # ---------------------------------------------------------------------------
# DISPLAY_TIMEFRAMES = ["1m", "15m", "1h", "4h"]

# TIMEFRAME_DISPLAY = {
#     "1m": {"label": "1m", "color": "sky"},
#     "15m": {"label": "15m", "color": "amber"},
#     "1h": {"label": "1h", "color": "violet"},
#     "4h": {"label": "4h", "color": "rose"},
# }

# # ---------------------------------------------------------------------------
# # Indicator Registry — defines what to calculate + how to display it
# # ---------------------------------------------------------------------------
# # Each entry has:
# #   params      — passed to _calc_{name}(df, **params)
# #   timeframes  — which TFs to calculate on
# #   order       — display order within a timeframe column (lower = top)
# #   multi       — True if indicator produces multiple outputs
# #
# # For single-output indicators:
# #   label, format, decimals, bold, font_size, color_rules, default_class
# #
# # For multi-output indicators:
# #   slots — list of {suffix, label, format, decimals, bold, font_size,
# #                     color_rules, default_class}
# #   Each slot maps to one output key: {tf}_{name}_{suffix}
# #
# # Format types:
# #   "decimal"  — toFixed(decimals)                          e.g. RSI 62.4
# #   "price"    — $XX,XXX locale string                       e.g. EMA $98,900
# #   "signed"   — +/- prefix + toFixed(decimals)             e.g. MACD +12.50
# #   "volume"   — K/M suffixes                               e.g. VolSM 1.2M
# #
# # Color rules: [{"op": "gt"|"lt", "value": number, "class": "tailwind-class"}]
# #   First matching rule wins. Falls back to default_class.
# # ---------------------------------------------------------------------------
# INDICATOR_REGISTRY: Dict[str, Dict[str, Any]] = {
#     # ── RSI ──────────────────────────────────────────────
#     "rsi": {
#         "timeframes": ["1m", "15m", "1h"],
#         "params": {"length": 14},
#         "order": 0,
#         "label": "RSI",
#         "format": "decimal",
#         "decimals": 1,
#         "bold": True,
#         "font_size": "12px",
#         "color_rules": [
#             {"op": "gt", "value": 70, "class": "text-red-400"},
#             {"op": "lt", "value": 30, "class": "text-emerald-400"},
#         ],
#         "default_class": "text-gray-200 dark:text-gray-300",
#     },

#     # ── EMA (multi: 9, 21) ──────────────────────────────
#     "ema": {
#         "timeframes": ["1m", "15m", "1h", "4h"],
#         "params": {"length": [9, 21]},
#         "order": 1,
#         "multi": True,
#         "slots": [
#             {"suffix": "9", "label": "EMA9", "format": "price", "decimals": 0,
#              "font_size": "11px", "default_class": "text-gray-300 dark:text-gray-400"},
#             {"suffix": "21", "label": "EMA21", "format": "price", "decimals": 0,
#              "font_size": "11px", "default_class": "text-gray-300 dark:text-gray-400"},
#         ],
#     },

#     # ── SMA (multi: 20, 50) ─────────────────────────────
#     "sma": {
#         "timeframes": ["1m", "15m", "1h"],
#         "params": {"length": [20, 50]},
#         "order": 2,
#         "multi": True,
#         "slots": [
#             {"suffix": "20", "label": "SMA20", "format": "price", "decimals": 0,
#              "font_size": "11px", "default_class": "text-gray-300 dark:text-gray-400"},
#             {"suffix": "50", "label": "SMA50", "format": "price", "decimals": 0,
#              "font_size": "11px", "default_class": "text-gray-300 dark:text-gray-400"},
#         ],
#     },

#     # ── Bollinger Bands (multi: upper, mid, lower) ───────
#     "bbands": {
#         "timeframes": ["15m", "1h"],
#         "params": {"length": 20, "std": 2},
#         "order": 3,
#         "multi": True,
#         "slots": [
#             {"suffix": "upper", "label": "BB-U", "format": "price", "decimals": 0,
#              "font_size": "10px", "default_class": "text-gray-300 dark:text-gray-400"},
#             {"suffix": "mid", "label": "BB-M", "format": "price", "decimals": 0,
#              "font_size": "10px", "default_class": "text-gray-300 dark:text-gray-400"},
#             {"suffix": "lower", "label": "BB-L", "format": "price", "decimals": 0,
#              "font_size": "10px", "default_class": "text-gray-300 dark:text-gray-400"},
#         ],
#     },

#     # ── ATR ─────────────────────────────────────────────
#     "atr": {
#         "timeframes": ["15m", "1h"],
#         "params": {"length": 14},
#         "order": 4,
#         "label": "ATR",
#         "format": "price",
#         "decimals": 2,
#         "font_size": "11px",
#         "default_class": "text-gray-300 dark:text-gray-400",
#     },

#     # ── Stochastic (multi: k, d) ────────────────────────
#     "stoch": {
#         "timeframes": ["15m", "1h"],
#         "params": {"k": 14, "d": 3, "smooth_k": 3},
#         "order": 5,
#         "multi": True,
#         "slots": [
#             {"suffix": "k", "label": "StoK", "format": "decimal", "decimals": 1,
#              "font_size": "10px",
#              "color_rules": [
#                  {"op": "gt", "value": 80, "class": "text-red-400"},
#                  {"op": "lt", "value": 20, "class": "text-emerald-400"},
#              ],
#              "default_class": "text-gray-300 dark:text-gray-400"},
#             {"suffix": "d", "label": "StoD", "format": "decimal", "decimals": 1,
#              "font_size": "10px",
#              "color_rules": [
#                  {"op": "gt", "value": 80, "class": "text-red-400"},
#                  {"op": "lt", "value": 20, "class": "text-emerald-400"},
#              ],
#              "default_class": "text-gray-300 dark:text-gray-400"},
#         ],
#     },

#     # ── ADX (multi: adx, dmp, dmn) ──────────────────────
#     "adx": {
#         "timeframes": ["1h", "4h"],
#         "params": {"length": 14},
#         "order": 6,
#         "multi": True,
#         "slots": [
#             {"suffix": "adx", "label": "ADX", "format": "decimal", "decimals": 1,
#              "font_size": "11px",
#              "color_rules": [
#                  {"op": "gt", "value": 25, "class": "text-amber-400"},
#              ],
#              "default_class": "text-gray-300 dark:text-gray-400"},
#             {"suffix": "dmp", "label": "+DI", "format": "decimal", "decimals": 1,
#              "font_size": "10px",
#              "default_class": "text-emerald-400/70"},
#             {"suffix": "dmn", "label": "-DI", "format": "decimal", "decimals": 1,
#              "font_size": "10px",
#              "default_class": "text-red-400/70"},
#         ],
#     },

#     # ── MACD (multi: hist, line, signal) ────────────────
#     "macd": {
#         "timeframes": ["1h", "4h"],
#         "params": {"fast": 12, "slow": 26, "signal": 9},
#         "order": 7,
#         "multi": True,
#         "slots": [
#             {"suffix": "hist", "label": "MACD", "format": "signed", "decimals": 2,
#              "font_size": "10px",
#              "color_rules": [
#                  {"op": "gt", "value": 0, "class": "text-emerald-400"},
#                  {"op": "lt", "value": 0, "class": "text-red-400"},
#              ],
#              "default_class": "text-gray-300 dark:text-gray-400"},
#             {"suffix": "line", "label": "MACD-L", "format": "signed", "decimals": 2,
#              "font_size": "10px",
#              "default_class": "text-gray-300 dark:text-gray-400"},
#             {"suffix": "signal", "label": "MACD-S", "format": "signed", "decimals": 2,
#              "font_size": "10px",
#              "default_class": "text-gray-300 dark:text-gray-400"},
#         ],
#     },

#     # ── Volume SMA ──────────────────────────────────────
#     "volume_sma": {
#         "timeframes": ["1h"],
#         "params": {"length": 20},
#         "order": 8,
#         "label": "VolSM",
#         "format": "volume",
#         "decimals": 0,
#         "font_size": "10px",
#         "default_class": "text-gray-300 dark:text-gray-400",
#     },
# }

# # ---------------------------------------------------------------------------
# # Strategy Definitions (pandas-ta Strategy Pattern)
# # ---------------------------------------------------------------------------
# # Each strategy is a ta.Strategy that composes indicators and runs via
# # df.ta.strategy(). The engine extracts column values and passes them
# # to a _decide_{key} method for bull/bear/sideways classification.
# #
# # Adding a new strategy:
# #   1. Define ta.Strategy(...) with indicator list
# #   2. Add entry to STRATEGY_REGISTRY below
# #   3. Add _decide_{key}() method on IndicatorEngine
# #   4. Wire it in _decide_strategy() dispatcher
# #   5. Done — appears on all asset cards automatically.
# # ---------------------------------------------------------------------------

# MomentumStrategy = ta.Strategy(
#     name="Momentum",
#     description="Short-term momentum: RSI + MACD + Stochastic",
#     ta=[
#         {"kind": "rsi", "length": 14},
#         {"kind": "macd", "fast": 12, "slow": 26, "signal": 9},
#         {"kind": "stoch", "k": 14, "d": 3, "smooth_k": 3},
#     ],
# )

# TrendStrategy = ta.Strategy(
#     name="Trend",
#     description="Trend direction and strength: EMA stack + SMA50 + ADX",
#     ta=[
#         {"kind": "ema", "length": 9},
#         {"kind": "ema", "length": 21},
#         {"kind": "sma", "length": 50},
#         {"kind": "adx", "length": 14},
#     ],
# )

# VolatilityStrategy = ta.Strategy(
#     name="Volatility",
#     description="Mean reversion: Bollinger Bands position + ATR",
#     ta=[
#         {"kind": "bbands", "length": 20},
#         {"kind": "atr", "length": 14},
#     ],
# )

# STRATEGY_REGISTRY: List[Dict[str, Any]] = [
#     {
#         "key": "momentum",
#         "strategy": MomentumStrategy,
#         "timeframes": ["1h", "15m"],  # Try 1h first, fallback to 15m
#         "display": {"label": "Mom", "order": 0},
#     },
#     {
#         "key": "trend",
#         "strategy": TrendStrategy,
#         "timeframes": ["4h", "1h"],  # Try 4h first, fallback to 1h
#         "display": {"label": "Trend", "order": 1},
#     },
#     {
#         "key": "volatility",
#         "strategy": VolatilityStrategy,
#         "timeframes": ["1h", "15m"],
#         "display": {"label": "Vol", "order": 2},
#     },
#     # ── Multi-Timeframe Strategy ────────────────────────────────────
#     # Fast Short-Trend: EMA13/WMA44 crossover across 3 timeframes.
#     # Original concept: 15m → 1h → 1d.  Scaled to available data:
#     #   Primary  = 5m  (EMA13 × WMA44 crossover + near/crossing detection)
#     #   Mid      = 15m (EMA13_5m vs WMA44_15m alignment)
#     #   High     = 4h  (EMA13_5m vs WMA44_4h alignment)
#     #   Price filter: price > WMA44 for last 2-3 candles on 15m and 4h.
#     #
#     # Ratios preserved:  5m:15m = 1:3  (orig 15m:1h = 1:4)
#     #                    5m:4h  = 1:48 (orig 15m:1d = 1:96)
#     #                    15m:4h = 1:16 (orig 1h:1d  = 1:24)
#     #
#     # multi_timeframe=True → engine passes candles_by_tf directly
#     # instead of running df.ta.strategy() on a single TF.
#     {
#         "key": "fast_trend",
#         "timeframes": ["5m", "15m", "4h"],
#         "display": {"label": "FST", "order": 3},
#         "multi_timeframe": True,
#     },
# ]


# class IndicatorEngine:
#     """
#     Modular technical indicator engine.
#     Calculates indicators from OHLCV candles using pandas-ta.
#     """

#     def __init__(self):
#         self.registry = INDICATOR_REGISTRY.copy()

#     # ------------------------------------------------------------------
#     # Display Configuration — consumed by frontend for dynamic rendering
#     # ------------------------------------------------------------------

#     @staticmethod
#     def get_display_config() -> dict:
#         """
#         Generate the full indicator display configuration for the frontend.
#         Returns a JSON-serializable dict that the frontend uses to
#         dynamically render all indicator rows — no hardcoded HTML needed.

#         Response structure:
#         {
#             "timeframes": [
#                 {"key": "1m", "label": "1m", "color": "sky"},
#                 ...
#             ],
#             "grid_columns": 4,
#             "indicators": {
#                 "1m": [
#                     {
#                         "key": "1m_rsi_14",
#                         "label": "RSI",
#                         "format": "decimal",
#                         "decimals": 1,
#                         "bold": true,
#                         "font_size": "12px",
#                         "color_rules": [...],
#                         "default_class": "..."
#                     },
#                     ...
#                 ],
#                 "15m": [...],
#                 "1h": [...],
#                 "4h": [...]
#             }
#         }
#         """
#         config = {
#             "timeframes": [],
#             "grid_columns": len(DISPLAY_TIMEFRAMES),
#             "indicators": {},
#         }

#         for tf_key in DISPLAY_TIMEFRAMES:
#             tf_display = TIMEFRAME_DISPLAY.get(tf_key, {})
#             config["timeframes"].append({
#                 "key": tf_key,
#                 "label": tf_display.get("label", tf_key),
#                 "color": tf_display.get("color", "gray"),
#             })

#             # Collect all indicator slots for this timeframe, sorted by order
#             slots = []
#             for ind_name, ind_cfg in INDICATOR_REGISTRY.items():
#                 if tf_key not in ind_cfg.get("timeframes", []):
#                     continue

#                 if ind_cfg.get("multi"):
#                     # Multi-output: iterate slots
#                     for slot in ind_cfg.get("slots", []):
#                         slot_entry = {
#                             "key": f"{tf_key}_{ind_name}_{slot['suffix']}",
#                             "label": slot["label"],
#                             "format": slot["format"],
#                             "decimals": slot["decimals"],
#                             "bold": slot.get("bold", False),
#                             "font_size": slot["font_size"],
#                             "color_rules": slot.get("color_rules", []),
#                             "default_class": slot["default_class"],
#                         }
#                         slots.append((ind_cfg.get("order", 99), slot_entry))
#                 else:
#                     # Single-output
#                     slot_entry = {
#                         "key": f"{tf_key}_{ind_name}_{_param_str(ind_cfg['params'])}",
#                         "label": ind_cfg["label"],
#                         "format": ind_cfg["format"],
#                         "decimals": ind_cfg["decimals"],
#                         "bold": ind_cfg.get("bold", False),
#                         "font_size": ind_cfg["font_size"],
#                         "color_rules": ind_cfg.get("color_rules", []),
#                         "default_class": ind_cfg["default_class"],
#                     }
#                     slots.append((ind_cfg.get("order", 99), slot_entry))

#             # Sort by order (stable — preserves insertion order for ties)
#             slots.sort(key=lambda x: x[0])
#             config["indicators"][tf_key] = [s[1] for s in slots]

#         return config

#     # ------------------------------------------------------------------
#     # Strategy Engine — multi-strategy decisions using ta.Strategy
#     # ------------------------------------------------------------------

#     def compute_strategies(
#         self, candles_by_tf: Dict[str, List[Dict]], price: float = None
#     ) -> list:
#         """
#         Run each registered strategy using df.ta.strategy() and return decisions.

#         Uses pandas-ta Strategy pattern for clean indicator composition.
#         Each strategy runs on its preferred timeframe, extracts indicator
#         values from the DataFrame, and applies strategy-specific rules.

#         Returns:
#             [
#                 {
#                     "key": "momentum",
#                     "name": "Momentum",
#                     "label": "BULLISH",
#                     "color": "emerald",
#                     "bull_score": 3,
#                     "bear_score": 0,
#                     "rules_matched": ["RSI_oversold", "MACD_bull", "Stoch_oversold"],
#                     "display": {"label": "Mom", "order": 0},
#                     "timeframe": "1h",
#                 },
#                 ...
#             ]
#         """
#         results = []

#         for strat_cfg in STRATEGY_REGISTRY:
#             key = strat_cfg["key"]
#             tfs = strat_cfg["timeframes"]
#             display = strat_cfg["display"]
#             is_mtf = strat_cfg.get("multi_timeframe", False)

#             # ── Multi-timeframe strategy: pass full candles_by_tf ──
#             if is_mtf:
#                 try:
#                     decision = self._decide_strategy_mtf(
#                         key, candles_by_tf, price=price
#                     )
#                 except Exception as e:
#                     logger.debug(f"Multi-TF strategy '{key}' failed: {e}")
#                     decision = {
#                         "label": "—", "color": "gray",
#                         "bull_score": 0, "bear_score": 0, "rules_matched": [],
#                     }
#                 results.append({
#                     "key": key,
#                     "name": f"{display.get('label', key)} (Short)",
#                     "label": decision["label"],
#                     "color": decision["color"],
#                     "bull_score": decision["bull_score"],
#                     "bear_score": decision["bear_score"],
#                     "rules_matched": decision.get("rules_matched", []),
#                     "display": display,
#                     "timeframe": "+".join(tfs),
#                 })
#                 continue

#             # ── Single-timeframe strategy (standard df.ta.strategy) ──
#             strategy = strat_cfg["strategy"]
#             extracted = None
#             used_tf = None
#             for tf in tfs:
#                 candles = candles_by_tf.get(tf, [])
#                 if len(candles) < 50:
#                     continue
#                 df = self._candles_to_dataframe(candles)
#                 if df is None or len(df) < 50:
#                     continue

#                 try:
#                     df_copy = df.copy()
#                     df_copy.ta.cores = 0  # No multiprocessing in Celery worker
#                     df_copy.ta.strategy(strategy)

#                     # Extract last row values (skip OHLCV columns)
#                     last = df_copy.iloc[-1]
#                     extracted = {}
#                     for col in df_copy.columns:
#                         if col in ("open", "high", "low", "close", "volume"):
#                             continue
#                         val = last[col]
#                         if val is not None and not (isinstance(val, float) and val != val):
#                             extracted[col] = float(val)

#                     if extracted:
#                         used_tf = tf
#                         break
#                 except Exception as e:
#                     logger.debug(f"Strategy '{key}' failed on {tf}: {e}")
#                     continue

#             # Apply strategy-specific decision rules
#             if extracted:
#                 decision = self._decide_strategy(key, extracted, price=price)
#             else:
#                 decision = {
#                     "label": "—", "color": "gray",
#                     "bull_score": 0, "bear_score": 0, "rules_matched": [],
#                 }

#             results.append({
#                 "key": key,
#                 "name": strategy.name,
#                 "label": decision["label"],
#                 "color": decision["color"],
#                 "bull_score": decision["bull_score"],
#                 "bear_score": decision["bear_score"],
#                 "rules_matched": decision.get("rules_matched", []),
#                 "display": display,
#                 "timeframe": used_tf,
#             })

#         # Sort by display order
#         results.sort(key=lambda x: x.get("display", {}).get("order", 99))
#         return results

#     def _decide_strategy(self, key: str, values: dict, price: float = None) -> dict:
#         """Route to the appropriate decision function for this strategy."""
#         decider = getattr(self, f"_decide_{key}", None)
#         if decider:
#             return decider(values, price=price)
#         return {
#             "label": "NEUTRAL", "color": "gray",
#             "bull_score": 0, "bear_score": 0, "rules_matched": [],
#         }

#     def _decide_momentum(self, values: dict, price: float = None) -> dict:
#         """
#         Momentum Strategy Decision.
#         Uses RSI, MACD histogram, and Stochastic K across one timeframe.

#         Rules:
#           RSI < 30 = bullish (oversold),  RSI > 70 = bearish (overbought)
#           MACD hist > 0 = bullish,          MACD hist < 0 = bearish
#           Stoch K < 20 = bullish (oversold), Stoch K > 80 = bearish (overbought)
#         """
#         bull, bear, matched = 0, 0, []

#         # RSI
#         rsi = _find_value(values, ["RSI_"])
#         if rsi is not None:
#             if rsi < 30:
#                 bull += 1
#                 matched.append("RSI_oversold")
#             elif rsi > 70:
#                 bear += 1
#                 matched.append("RSI_overbought")

#         # MACD histogram
#         macd_hist = _find_value(values, ["MACDh_"])
#         if macd_hist is not None:
#             if macd_hist > 0:
#                 bull += 1
#                 matched.append("MACD_bull")
#             elif macd_hist < 0:
#                 bear += 1
#                 matched.append("MACD_bear")

#         # Stochastic K
#         stoch_k = _find_value(values, ["STOCHk"])
#         if stoch_k is not None:
#             if stoch_k < 20:
#                 bull += 1
#                 matched.append("Stoch_oversold")
#             elif stoch_k > 80:
#                 bear += 1
#                 matched.append("Stoch_overbought")

#         label, color = _classify_decision(bull, bear)
#         return {
#             "label": label, "color": color,
#             "bull_score": bull, "bear_score": bear, "rules_matched": matched,
#         }

#     def _decide_trend(self, values: dict, price: float = None) -> dict:
#         """
#         Trend Strategy Decision.
#         Uses EMA9/EMA21 stack, SMA50 position, and ADX strength.

#         Rules:
#           Price > EMA9 > EMA21 = strong bullish (2 pts)
#           Price < EMA9 < EMA21 = strong bearish (2 pts)
#           EMA9 > EMA21 (no price) = mild bull/bear (1 pt)
#           Price > SMA50 = bullish,  Price < SMA50 = bearish
#           ADX < 20 = informational (sideways) — doesn't score
#         """
#         bull, bear, matched = 0, 0, []

#         ema9 = _find_value(values, ["EMA_9"])
#         ema21 = _find_value(values, ["EMA_21"])
#         sma50 = _find_value(values, ["SMA_50"])
#         adx = _find_value(values, ["ADX_"])

#         # EMA stack with price
#         if price is not None and ema9 is not None and ema21 is not None:
#             if price > ema9 > ema21:
#                 bull += 2
#                 matched.append("Price>EMA9>EMA21")
#             elif price < ema9 < ema21:
#                 bear += 2
#                 matched.append("Price<EMA9<EMA21")
#         elif ema9 is not None and ema21 is not None:
#             if ema9 > ema21:
#                 bull += 1
#                 matched.append("EMA9>EMA21")
#             else:
#                 bear += 1
#                 matched.append("EMA9<EMA21")

#         # SMA50 position
#         if price is not None and sma50 is not None:
#             if price > sma50:
#                 bull += 1
#                 matched.append("Price>SMA50")
#             else:
#                 bear += 1
#                 matched.append("Price<SMA50")

#         # ADX strength (informational — doesn't score but adds context)
#         if adx is not None:
#             if adx < 20:
#                 matched.append("ADX_weak_trend")
#             elif adx > 25:
#                 matched.append("ADX_strong_trend")

#         label, color = _classify_decision(bull, bear)
#         return {
#             "label": label, "color": color,
#             "bull_score": bull, "bear_score": bear, "rules_matched": matched,
#         }

#     def _decide_volatility(self, values: dict, price: float = None) -> dict:
#         """
#         Volatility / Mean Reversion Strategy Decision.
#         Uses Bollinger Band position relative to price.

#         Rules:
#           Price ≤ BB lower = strongly bullish (oversold, 2 pts)
#           Price ≥ BB upper = strongly bearish (overbought, 2 pts)
#           Price < BB mid = mild bullish (1 pt)
#           Price > BB mid = mild bearish (1 pt)
#         """
#         bull, bear, matched = 0, 0, []

#         bb_lower = _find_value(values, ["BBL_"])
#         bb_upper = _find_value(values, ["BBU_"])
#         bb_mid = _find_value(values, ["BBM_"])

#         if price is not None and bb_lower is not None and bb_upper is not None:
#             if price <= bb_lower:
#                 bull += 2
#                 matched.append("BB_oversold")
#             elif price >= bb_upper:
#                 bear += 2
#                 matched.append("BB_overbought")
#             elif bb_mid is not None:
#                 if price < bb_mid:
#                     bull += 1
#                     matched.append("BB_below_mid")
#                 else:
#                     bear += 1
#                     matched.append("BB_above_mid")

#         label, color = _classify_decision(bull, bear)
#         return {
#             "label": label, "color": color,
#             "bull_score": bull, "bear_score": bear, "rules_matched": matched,
#         }

#     # ------------------------------------------------------------------
#     # Multi-Timeframe Strategy Dispatcher
#     # ------------------------------------------------------------------

#     def _decide_strategy_mtf(
#         self, key: str, candles_by_tf: Dict[str, List[Dict]], price: float = None
#     ) -> dict:
#         """
#         Route multi-timeframe strategies to their handler.
#         Receives the full candles_by_tf dict so the handler can
#         compute indicators across multiple timeframes.
#         """
#         decider = getattr(self, f"_decide_mtf_{key}", None)
#         if decider:
#             return decider(candles_by_tf, price=price)
#         return {
#             "label": "NEUTRAL", "color": "gray",
#             "bull_score": 0, "bear_score": 0, "rules_matched": [],
#         }

#     def _decide_mtf_fast_trend(
#         self, candles_by_tf: Dict[str, List[Dict]], price: float = None
#     ) -> dict:
#         """
#         Fast Short-Trend (FST): Multi-timeframe EMA13/WMA44 strategy.

#         Logic (scaled from original 15m→1h→1d concept):
#           ┌─────────────────────────────────────────────────────────────┐
#           │ PRIMARY (5m):  EMA13(5m) vs WMA44(5m)                     │
#           │   - Must be very near / crossing up   → +2 bull            │
#           │   - Must be > (not near, already wide)  → +1 bull           │
#           │   - Opposite                           → bear scores        │
#           │                                                                 │
#           │ MID (15m):     EMA13(5m) vs WMA44(15m)                     │
#           │   - EMA13(5m) > WMA44(15m)            → +1 bull            │
#           │   - Opposite                           → +1 bear            │
#           │                                                                 │
#           │ HIGH (4h):      EMA13(5m) vs WMA44(4h)                      │
#           │   - EMA13(5m) > WMA44(4h)             → +1 bull            │
#           │   - Opposite                           → +1 bear            │
#           │                                                                 │
#           │ PRICE FILTER (15m + 4h):                                        │
#           │   - Price > WMA44 on last 2-3 candles  → +1 bull per TF     │
#           │   - Price < WMA44 on last 2-3 candles  → +1 bear per TF     │
#           └─────────────────────────────────────────────────────────────┘

#         Scoring:
#           ≥4 bull, bear ≤ 1  → BULLISH
#           ≥3 bull, bear ≤ 0  → LEAN BULL
#           ≥4 bear, bull ≤ 1  → BEARISH
#           ≥3 bear, bull ≤ 0  → LEAN BEAR
#           else               → NEUTRAL
#         """
#         bull, bear, matched = 0, 0, []

#         # ── Helper: compute EMA and WMA series from candles ──
#         def _calc_ema_series(candles, length):
#             if len(candles) < length:
#                 return None
#             df = self._candles_to_dataframe(candles)
#             if df is None:
#                 return None
#             try:
#                 return ta.ema(df["close"], length=length)
#             except Exception:
#                 return None

#         def _calc_wma_series(candles, length):
#             if len(candles) < length:
#                 return None
#             df = self._candles_to_dataframe(candles)
#             if df is None:
#                 return None
#             try:
#                 return ta.wma(df["close"], length=length)
#             except Exception:
#                 return None

#         def _get_close_series(candles):
#             if len(candles) < 3:
#                 return None
#             df = self._candles_to_dataframe(candles)
#             if df is None:
#                 return None
#             return df["close"]

#         # ── 1) PRIMARY (5m): EMA13 vs WMA44 crossover ──
#         candles_5m = candles_by_tf.get("5m", [])
#         ema13_5m = _calc_ema_series(candles_5m, 13)
#         wma44_5m = _calc_wma_series(candles_5m, 44)

#         if ema13_5m is not None and wma44_5m is not None:
#             # Drop NaN to get valid index range
#             valid = ema13_5m.notna() & wma44_5m.notna()
#             if valid.sum() >= 2:
#                 cur_ema = float(ema13_5m[valid].iloc[-1])
#                 cur_wma = float(wma44_5m[valid].iloc[-1])
#                 prev_ema = float(ema13_5m[valid].iloc[-2])
#                 prev_wma = float(wma44_5m[valid].iloc[-2])

#                 # "Very near" = within 0.3% of each other
#                 gap_pct = abs(cur_ema - cur_wma) / cur_wma * 100 if cur_wma != 0 else 999
#                 is_near = gap_pct < 0.3

#                 # Crossing up: EMA was below or near WMA, now above
#                 crossing_up = (
#                     prev_ema <= prev_wma and cur_ema > cur_wma
#                 ) or (
#                     is_near and prev_ema < cur_ema > cur_wma
#                 )
#                 # Crossing down (bearish mirror)
#                 crossing_down = (
#                     prev_ema >= prev_wma and cur_ema < cur_wma
#                 ) or (
#                     is_near and prev_ema > cur_ema < cur_wma
#                 )

#                 if crossing_up:
#                     bull += 2
#                     matched.append("5m_cross_up")
#                 elif crossing_down:
#                     bear += 2
#                     matched.append("5m_cross_down")
#                 elif cur_ema > cur_wma:
#                     bull += 1
#                     matched.append("5m_EMA>WMA")
#                 elif cur_ema < cur_wma:
#                     bear += 1
#                     matched.append("5m_EMA<WMA")
#                 else:
#                     matched.append("5m_flat")

#         # ── 2) MID (15m): EMA13(5m) vs WMA44(15m) alignment ──
#         candles_15m = candles_by_tf.get("15m", [])
#         wma44_15m = _calc_wma_series(candles_15m, 44)

#         if ema13_5m is not None and wma44_15m is not None:
#             valid_ema = ema13_5m.notna()
#             valid_wma = wma44_15m.notna()
#             if valid_ema.sum() >= 1 and valid_wma.sum() >= 1:
#                 cur_ema_5m = float(ema13_5m[valid_ema].iloc[-1])
#                 cur_wma_15m = float(wma44_15m[valid_wma].iloc[-1])

#                 if cur_ema_5m > cur_wma_15m:
#                     bull += 1
#                     matched.append("15m_aligned")
#                 elif cur_ema_5m < cur_wma_15m:
#                     bear += 1
#                     matched.append("15m_misaligned")

#         # ── 3) HIGH (4h): EMA13(5m) vs WMA44(4h) alignment ──
#         candles_4h = candles_by_tf.get("4h", [])
#         wma44_4h = _calc_wma_series(candles_4h, 44)

#         if ema13_5m is not None and wma44_4h is not None:
#             valid_ema = ema13_5m.notna()
#             valid_wma = wma44_4h.notna()
#             if valid_ema.sum() >= 1 and valid_wma.sum() >= 1:
#                 cur_ema_5m = float(ema13_5m[valid_ema].iloc[-1])
#                 cur_wma_4h = float(wma44_4h[valid_wma].iloc[-1])

#                 if cur_ema_5m > cur_wma_4h:
#                     bull += 1
#                     matched.append("4h_aligned")
#                 elif cur_ema_5m < cur_wma_4h:
#                     bear += 1
#                     matched.append("4h_misaligned")

#         # ── 4) PRICE FILTER: price vs WMA44 on 15m and 4h ──
#         lookback = 3  # check last 3 candles

#         # 15m price filter
#         close_15m = _get_close_series(candles_15m)
#         if close_15m is not None and wma44_15m is not None:
#             valid_c = close_15m.notna()
#             valid_w = wma44_15m.notna()
#             overlap = valid_c & valid_w
#             if overlap.sum() >= lookback:
#                 above_count = 0
#                 below_count = 0
#                 for i in range(-lookback, 0):
#                     c = float(close_15m[overlap].iloc[i])
#                     w = float(wma44_15m[overlap].iloc[i])
#                     if c > w:
#                         above_count += 1
#                     elif c < w:
#                         below_count += 1
#                 # Require majority (2 out of 3)
#                 if above_count >= 2:
#                     bull += 1
#                     matched.append("15m_price_above")
#                 elif below_count >= 2:
#                     bear += 1
#                     matched.append("15m_price_below")

#         # 4h price filter
#         close_4h = _get_close_series(candles_4h)
#         if close_4h is not None and wma44_4h is not None:
#             valid_c = close_4h.notna()
#             valid_w = wma44_4h.notna()
#             overlap = valid_c & valid_w
#             if overlap.sum() >= lookback:
#                 above_count = 0
#                 below_count = 0
#                 for i in range(-lookback, 0):
#                     c = float(close_4h[overlap].iloc[i])
#                     w = float(wma44_4h[overlap].iloc[i])
#                     if c > w:
#                         above_count += 1
#                     elif c < w:
#                         below_count += 1
#                 if above_count >= 2:
#                     bull += 1
#                     matched.append("4h_price_above")
#                 elif below_count >= 2:
#                     bear += 1
#                     matched.append("4h_price_below")

#         # ── 5) Classify ──
#         # Multi-TF has more components — use slightly higher thresholds
#         if bull >= 4 and bear <= 1:
#             label, color = "BULLISH", "emerald"
#         elif bear >= 4 and bull <= 1:
#             label, color = "BEARISH", "red"
#         elif bull >= 3 and bear <= 0:
#             label, color = "LEAN BULL", "emerald"
#         elif bear >= 3 and bull <= 0:
#             label, color = "LEAN BEAR", "red"
#         elif bull > bear:
#             label, color = "LEAN BULL", "emerald"
#         elif bear > bull:
#             label, color = "LEAN BEAR", "red"
#         else:
#             label, color = "NEUTRAL", "gray"

#         return {
#             "label": label, "color": color,
#             "bull_score": bull, "bear_score": bear, "rules_matched": matched,
#         }

#     # ------------------------------------------------------------------
#     # Public API: Calculation
#     # ------------------------------------------------------------------

#     def calculate_all(
#         self, candles_by_tf: Dict[str, List[Dict]]
#     ) -> Dict[str, float]:
#         """
#         Calculate all registered indicators for all timeframes.
#         Returns flat key-value pairs: { "1m_rsi_14": 62.4, "1h_ema_9": 98900, ... }
#         """
#         results = {}

#         for indicator_name, config in self.registry.items():
#             handler = getattr(self, f"_calc_{indicator_name}", None)
#             if not handler:
#                 logger.warning(f"No handler for indicator '{indicator_name}', skipping")
#                 continue

#             for tf in config.get("timeframes", []):
#                 candles = candles_by_tf.get(tf, [])
#                 if len(candles) < FALLBACK_MIN_ROWS:
#                     continue

#                 try:
#                     df = self._candles_to_dataframe(candles)
#                     if df is None or len(df) < FALLBACK_MIN_ROWS:
#                         continue

#                     # --- Minimum data validation: skip BEFORE calling pandas-ta ---
#                     params = config.get("params", {})
#                     min_fn = MIN_ROWS.get(indicator_name)
#                     required = min_fn(params) if min_fn else FALLBACK_MIN_ROWS
#                     if len(df) < required:
#                         logger.debug(
#                             f"Skipping {indicator_name} on {tf}: "
#                             f"{len(df)} rows < {required} required"
#                         )
#                         continue

#                     if config.get("multi"):
#                         # Multi-output indicator — returns dict of {suffix: value}
#                         multi_results = handler(df, **params)
#                         if multi_results:
#                             for suffix, value in multi_results.items():
#                                 key = f"{tf}_{indicator_name}_{suffix}"
#                                 results[key] = round(float(value), 6) if value is not None else None
#                     else:
#                         # Single-output indicator
#                         value = handler(df, **params)
#                         if value is not None:
#                             param_str = _param_str(params)
#                             key = f"{tf}_{indicator_name}_{param_str}"
#                             results[key] = round(float(value), 6)

#                 except Exception as e:
#                     logger.debug(
#                         f"Indicator {indicator_name} failed on {tf}: {e}"
#                     )
#                     continue

#         return results

#     def calculate_all_with_strategy(
#         self, candles_by_tf: Dict[str, List[Dict]], price: float = None
#     ) -> dict:
#         """
#         Calculate indicators + multi-strategy decisions.
#         Used for WS broadcast — frontend consumes this directly.

#         Returns:
#             {
#                 "values": { "1m_rsi_14": 62.4, ... },            # indicator grid
#                 "strategies": [{ "key": "momentum", ... }, ...],   # strategy decisions
#             }
#         """
#         values = self.calculate_all(candles_by_tf)
#         strategies = self.compute_strategies(candles_by_tf, price=price)
#         return {
#             "values": values,
#             "strategies": strategies,
#         }

#     # ------------------------------------------------------------------
#     # Indicator Implementations (private, named _calc_{name})
#     # ------------------------------------------------------------------

#     def _calc_rsi(self, df: pd.DataFrame, length: int = 14) -> Optional[float]:
#         """Relative Strength Index."""
#         result = ta.rsi(df["close"], length=length)
#         if result is not None and len(result) > 0:
#             return float(result.iloc[-1])
#         return None

#     def _calc_ema(
#         self, df: pd.DataFrame, length: list = None
#     ) -> Dict[str, float]:
#         """Exponential Moving Average — multi-output."""
#         if not length:
#             length = [9, 21]
#         results = {}
#         for period in length:
#             result = ta.ema(df["close"], length=period)
#             if result is not None and len(result) > 0:
#                 val = float(result.iloc[-1])
#                 if val is not None:
#                     results[str(period)] = val
#         return results

#     def _calc_sma(
#         self, df: pd.DataFrame, length: list = None
#     ) -> Dict[str, float]:
#         """Simple Moving Average — multi-output."""
#         if not length:
#             length = [20, 50]
#         results = {}
#         for period in length:
#             result = ta.sma(df["close"], length=period)
#             if result is not None and len(result) > 0:
#                 val = float(result.iloc[-1])
#                 if val is not None:
#                     results[str(period)] = val
#         return results

#     def _calc_bbands(
#         self, df: pd.DataFrame, length: int = 20, std: int = 2
#     ) -> Dict[str, float]:
#         """Bollinger Bands — returns upper, mid, lower."""
#         result = ta.bbands(df["close"], length=length, std=std)
#         if result is not None:
#             row = result.iloc[-1]
#             results = {}
#             for col in result.columns:
#                 if col.startswith("BBL"):
#                     results["lower"] = float(row[col])
#                 elif col.startswith("BBM"):
#                     results["mid"] = float(row[col])
#                 elif col.startswith("BBU"):
#                     results["upper"] = float(row[col])
#             return results
#         return {}

#     def _calc_macd(
#         self, df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
#     ) -> Dict[str, float]:
#         """MACD — returns macd_line, signal_line, histogram."""
#         result = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
#         if result is not None and len(result) > 0:
#             row = result.iloc[-1]
#             results = {}
#             for col in result.columns:
#                 if col.startswith("MACD_") and "signal" not in col and "hist" not in col:
#                     results["line"] = float(row[col])
#                 elif "signal" in col:
#                     results["signal"] = float(row[col])
#                 elif "hist" in col:
#                     results["hist"] = float(row[col])
#             return results if results else {}
#         return {}

#     def _calc_atr(self, df: pd.DataFrame, length: int = 14) -> Optional[float]:
#         """Average True Range."""
#         result = ta.atr(df["high"], df["low"], df["close"], length=length)
#         if result is not None and len(result) > 0:
#             return float(result.iloc[-1])
#         return None

#     def _calc_stoch(
#         self, df: pd.DataFrame, k: int = 14, d: int = 3, smooth_k: int = 3
#     ) -> Dict[str, float]:
#         """Stochastic Oscillator — returns k and d values."""
#         result = ta.stoch(
#             df["high"], df["low"], df["close"],
#             k=k, d=d, smooth_k=smooth_k
#         )
#         if result is not None and len(result) > 0:
#             row = result.iloc[-1]
#             results = {}
#             for col in result.columns:
#                 if col.startswith("STOCHk"):
#                     results["k"] = float(row[col])
#                 elif col.startswith("STOCHd"):
#                     results["d"] = float(row[col])
#             return results if results else {}
#         return {}

#     def _calc_adx(self, df: pd.DataFrame, length: int = 14) -> Dict[str, float]:
#         """Average Directional Index — returns adx, dmp (+DI), dmn (-DI)."""
#         result = ta.adx(df["high"], df["low"], df["close"], length=length)
#         if result is not None and len(result) > 0:
#             row = result.iloc[-1]
#             results = {}
#             for col in result.columns:
#                 if col.startswith("ADX_"):
#                     results["adx"] = float(row[col])
#                 elif col.startswith("DMP_"):
#                     results["dmp"] = float(row[col])
#                 elif col.startswith("DMN_"):
#                     results["dmn"] = float(row[col])
#             return results if results else {}
#         return {}

#     def _calc_volume_sma(
#         self, df: pd.DataFrame, length: int = 20
#     ) -> Optional[float]:
#         """Volume Simple Moving Average."""
#         result = ta.sma(df["volume"], length=length)
#         if result is not None and len(result) > 0:
#             return float(result.iloc[-1])
#         return None

#     # ------------------------------------------------------------------
#     # Helpers
#     # ------------------------------------------------------------------

#     @staticmethod
#     def _candles_to_dataframe(candles: List[Dict]) -> Optional[pd.DataFrame]:
#         """
#         Convert list of candle dicts to a pandas DataFrame
#         with columns: open, high, low, close, volume.
#         """
#         if not candles:
#             return None

#         try:
#             df = pd.DataFrame(candles)
#             df = df.rename(columns={
#                 "o": "open",
#                 "h": "high",
#                 "l": "low",
#                 "c": "close",
#                 "v": "volume",
#             })
#             # Ensure numeric types
#             for col in ("open", "high", "low", "close", "volume"):
#                 df[col] = pd.to_numeric(df[col], errors="coerce")

#             # Drop rows with NaN OHLCV
#             df = df.dropna(subset=["open", "high", "low", "close"])

#             if len(df) == 0:
#                 return None

#             return df[["open", "high", "low", "close", "volume"]]

#         except Exception as e:
#             logger.warning(f"Failed to convert candles to DataFrame: {e}")
#             return None


# # ---------------------------------------------------------------------------
# # Module-level helpers
# # ---------------------------------------------------------------------------

# def _param_str(params: dict) -> str:
#     """Convert params dict to underscore-joined string for key generation."""
#     return "_".join(str(v) for v in params.values())


# def _find_value(values: dict, prefixes: list) -> Optional[float]:
#     """
#     Find the first value whose key matches any of the given prefixes.
#     Used to extract pandas-ta column values without hardcoding exact column names.
#     Example: _find_value(values, ["MACDh_"]) → finds MACDh_12_26_9
#     """
#     for col, val in values.items():
#         for prefix in prefixes:
#             if col.startswith(prefix):
#                 return val
#     return None


# def _classify_decision(bull: int, bear: int) -> tuple:
#     """Map bull/bear point scores to a (label, color) decision tuple."""
#     if bull >= 2 and bull > bear + 1:
#         return "BULLISH", "emerald"
#     elif bear >= 2 and bear > bull + 1:
#         return "BEARISH", "red"
#     elif bull >= 1 and bear <= 0:
#         return "LEAN BULL", "emerald"
#     elif bear >= 1 and bull <= 0:
#         return "LEAN BEAR", "red"
#     else:
#         return "NEUTRAL", "gray"

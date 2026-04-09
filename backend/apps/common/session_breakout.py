"""
Session Breakout Strategy (SBS)
================================
A multi-timeframe strategy that identifies breakout opportunities within
market sessions using Initial Balance (IB) ranges, CME gaps, and momentum
confirmation.

Architecture:
  - Pure function: receives all data as parameters (no Redis, no side effects).
  - Designed to be called from IndicatorEngine._decide_mtf_session_breakout()
    or directly from the orchestrator.
  - Returns a standard strategy decision dict PLUS sbs_data for frontend display.

Session Lifecycle:
  ┌──────────────────────────────────────────────────────────────────────┐
  │ Session Start                                                        │
  │ ├── 0–30 min  → IB Period (accumulation)                             │
  │ ├── 30 min    → IB Complete → Record IB_High, IB_Low, IB_Range       │
  │ ├── 30–120 min → Breakout Window (look for breakout)                  │
  │ │   ├── LONG:  Close > IB_High + body ≥ 0.5 + RSI > 55              │
  │ │   └── SHORT: Close < IB_Low  + body ≥ 0.5 + RSI < 45              │
  │ └── > 120 min → Post-Window (FADE mode if no breakout)               │
  └──────────────────────────────────────────────────────────────────────┘

Filters (both directions):
  - IB range ≥ 0.3 × ATR (not too flat)
  - CME gap not opposing by > 1× ATR

After Breakout:
  LONG  → TP1 = IB_H + Range×1.0,  TP2 = IB_H + Range×2.0,  SL = IB_L - Range×0.1
  SHORT → TP1 = IB_L - Range×1.0,  TP2 = IB_L - Range×2.0,  SL = IB_H + Range×0.1

CME Gap:
  Asian    → Previous = US (prior day)
  European → Previous = Asian (same day)
  US       → Previous = European (same day)
"""

import datetime
import logging
import math
from typing import Dict, List, Optional, Any

import pandas as pd
import pandas_ta_classic as ta

from .market_sessions import SESSIONS

logger = logging.getLogger("session.breakout")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IB_DURATION_MIN = 30          # Initial Balance period in minutes
BREAKOUT_WINDOW_MIN = 120     # Total window from session start (IB + 90 min)
BREAKOUT_BODY_MIN = 0.5       # Minimum breakout candle body (absolute price)
IB_RANGE_ATR_RATIO = 0.3      # IB range must be ≥ this × ATR
CME_GAP_ATR_LIMIT = 1.0       # CME gap opposing > this × ATR → skip
RSI_LONG_THRESHOLD = 55       # RSI(14) on 5m must be above this for LONG
RSI_SHORT_THRESHOLD = 45      # RSI(14) on 5m must be below this for SHORT
MIN_1M_FOR_IB = 2             # Minimum 1m candles to compute IB (edge case)
ATR_1H_LENGTH = 14            # ATR period for 1h candles
ATR_1M_LENGTH = 14            # ATR period for 1m fallback
RSI_5M_LENGTH = 14            # RSI period for 5m candles

# Session keys in chronological order within a UTC day
SESSION_ORDER = ["asian", "european", "us"]

# Map: current session → the session that precedes it (for CME gap)
PREVIOUS_SESSION_MAP = {
    "asian": "us",         # Asian → US of previous day
    "european": "asian",   # European → Asian of same day
    "us": "european",      # US → European of same day
}


# ---------------------------------------------------------------------------
# Helper: Safe DataFrame creation from candle dicts
# ---------------------------------------------------------------------------

def _candles_to_dataframe(candles: List[Dict]) -> Optional[pd.DataFrame]:
    """
    Convert list of candle dicts to a pandas DataFrame.
    Each dict has keys: t, o, h, l, c, v (and optionally T, q, n, V, Q).
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
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
        if len(df) == 0:
            return None
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.debug(f"SBS: candles_to_dataframe failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Session time helpers
# ---------------------------------------------------------------------------

def _ts_to_utc_hour(ts_ms: float) -> float:
    """Convert millisecond timestamp to UTC decimal hour."""
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000.0, tz=datetime.timezone.utc)
    return dt.hour + dt.minute / 60.0 + dt.second / 3600.0


def _ts_to_utc_date(ts_ms: float) -> datetime.date:
    """Convert millisecond timestamp to UTC date."""
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000.0, tz=datetime.timezone.utc)
    return dt.date()


def _session_start_ts(session_key: str, date: datetime.date) -> float:
    """
    Get the millisecond timestamp for session open on a given UTC date.
    """
    open_h = SESSIONS[session_key]["open_utc"]
    hours = int(open_h)
    minutes = int((open_h - hours) * 60)
    dt = datetime.datetime(date.year, date.month, date.day,
                            hours, minutes, 0,
                            tzinfo=datetime.timezone.utc)
    return dt.timestamp() * 1000.0


def _session_close_ts(session_key: str, date: datetime.date) -> float:
    """
    Get the millisecond timestamp for session close on a given UTC date.
    """
    close_h = SESSIONS[session_key]["close_utc"]
    hours = int(close_h)
    minutes = int((close_h - hours) * 60)
    dt = datetime.datetime(date.year, date.month, date.day,
                            hours, minutes, 0,
                            tzinfo=datetime.timezone.utc)
    return dt.timestamp() * 1000.0


# ---------------------------------------------------------------------------
# Candle filtering helpers
# ---------------------------------------------------------------------------

def get_session_candles(
    candles_1m: List[Dict],
    session_key: str,
    now_ts_ms: float,
) -> List[Dict]:
    """
    Filter 1m candles that belong to the current session.

    Uses session open_utc/close_utc from SESSIONS to determine bounds.
    Handles the case where session spans midnight (not currently, but future-proof).
    """
    if not candles_1m:
        return []

    cfg = SESSIONS[session_key]
    open_utc = cfg["open_utc"]
    close_utc = cfg["close_utc"]

    today = _ts_to_utc_date(now_ts_ms)
    start_ts = _session_start_ts(session_key, today)
    end_ts = _session_close_ts(session_key, today)

    filtered = [
        c for c in candles_1m
        if start_ts <= c["t"] < end_ts
    ]

    return filtered


def _get_previous_session_candles(
    candles_1m: List[Dict],
    current_session_key: str,
    now_ts_ms: float,
) -> List[Dict]:
    """
    Get 1m candles belonging to the previous session (for CME gap).

    For Asian → US of previous day
    For European → Asian of same day
    For US → European of same day
    """
    prev_key = PREVIOUS_SESSION_MAP.get(current_session_key)
    if not prev_key:
        return []

    today = _ts_to_utc_date(now_ts_ms)

    # Asian looks back to previous day for US session
    if current_session_key == "asian":
        target_date = today - datetime.timedelta(days=1)
    else:
        target_date = today

    return get_session_candles(candles_1m, prev_key, _session_close_ts(prev_key, target_date))


# ---------------------------------------------------------------------------
# IB computation
# ---------------------------------------------------------------------------

def compute_ib(
    candles_1m: List[Dict],
    session_key: str,
    now_ts_ms: float,
) -> Optional[Dict[str, Any]]:
    """
    Compute Initial Balance (IB) from the first IB_DURATION_MIN minutes
    of the current session.

    Returns dict with:
      - ib_high: float
      - ib_low: float
      - ib_mid: float
      - ib_range: float
      - ib_complete: bool
      - candles_used: int
    Or None if insufficient data.
    """
    session_candles = get_session_candles(candles_1m, session_key, now_ts_ms)
    if not session_candles:
        return None

    today = _ts_to_utc_date(now_ts_ms)
    session_start = _session_start_ts(session_key, today)
    ib_end = session_start + (IB_DURATION_MIN * 60 * 1000)

    # IB candles: those that started before ib_end
    # A candle at time T represents the interval [T, T+60000)
    ib_candles = [c for c in session_candles if c["t"] < ib_end]

    if not ib_candles:
        return None

    ib_high = max(c["h"] for c in ib_candles)
    ib_low = min(c["l"] for c in ib_candles)
    ib_range = ib_high - ib_low
    ib_mid = (ib_high + ib_low) / 2.0

    # IB is complete if current time is past ib_end
    # Use the last candle's close time to determine completeness
    last_candle_open = ib_candles[-1]["t"]
    last_candle_close = last_candle_open + 60000  # 1m candle
    ib_complete = last_candle_close >= ib_end and len(ib_candles) >= IB_DURATION_MIN

    # Also consider IB complete if we have enough candles and now_ts is past ib_end
    if now_ts_ms >= ib_end and len(ib_candles) >= MIN_1M_FOR_IB:
        ib_complete = True

    return {
        "ib_high": ib_high,
        "ib_low": ib_low,
        "ib_mid": ib_mid,
        "ib_range": ib_range,
        "ib_complete": ib_complete,
        "candles_used": len(ib_candles),
    }


# ---------------------------------------------------------------------------
# Progressive Middle Line
# ---------------------------------------------------------------------------

def compute_progressive_mid(session_candles: List[Dict]) -> Optional[float]:
    """
    Compute the Progressive Middle Line: (Running_Session_High + Running_Session_Low) / 2.

    Updates with every new candle that expands the session range.
    Acts as a dynamic S/R wall.

    Returns None if no session candles available.
    """
    if not session_candles:
        return None

    session_high = max(c["h"] for c in session_candles)
    session_low = min(c["l"] for c in session_candles)
    return (session_high + session_low) / 2.0


# ---------------------------------------------------------------------------
# CME Gap computation
# ---------------------------------------------------------------------------

def compute_cme_gap(
    candles_1m: List[Dict],
    session_key: str,
    now_ts_ms: float,
) -> Optional[float]:
    """
    Compute CME Gap = Current session open price - Previous session last close price.

    Returns None if data is insufficient.
    Positive gap = current session opened higher (bullish gap up).
    Negative gap = current session opened lower (bearish gap down).
    """
    # Current session open price = open of the first candle in the current session
    session_candles = get_session_candles(candles_1m, session_key, now_ts_ms)
    if not session_candles:
        return None

    current_open = session_candles[0]["o"]

    # Previous session last close = close of the last candle in the previous session
    prev_candles = _get_previous_session_candles(candles_1m, session_key, now_ts_ms)
    if not prev_candles:
        return None

    # Last close = close of the most recent candle in the previous session
    prev_close = prev_candles[-1]["c"]

    return current_open - prev_close


# ---------------------------------------------------------------------------
# ATR computation
# ---------------------------------------------------------------------------

def compute_atr(
    candles_by_tf: Dict[str, List[Dict]],
    candles_1m: List[Dict],
) -> Optional[float]:
    """
    Compute ATR for the strategy.

    Priority:
      1. Use 1h candles with pandas-ta ATR(14)
      2. Fallback: compute from 1m candles using simplified True Range

    Returns None if insufficient data.
    """
    # Try 1h candles first
    candles_1h = candles_by_tf.get("1h", [])
    if len(candles_1h) >= ATR_1H_LENGTH + 2:
        df = _candles_to_dataframe(candles_1h)
        if df is not None and len(df) >= ATR_1H_LENGTH + 2:
            try:
                atr_series = ta.atr(df["high"], df["low"], df["close"],
                                    length=ATR_1H_LENGTH)
                if atr_series is not None and len(atr_series) > 0:
                    val = atr_series.iloc[-1]
                    if not (isinstance(val, float) and math.isnan(val)):
                        return float(val)
            except Exception as e:
                logger.debug(f"SBS: 1h ATR failed: {e}")

    # Fallback: compute from 1m candles using simplified ATR
    if len(candles_1m) >= ATR_1M_LENGTH + 2:
        df = _candles_to_dataframe(candles_1m)
        if df is not None and len(df) >= ATR_1M_LENGTH + 2:
            try:
                atr_series = ta.atr(df["high"], df["low"], df["close"],
                                    length=ATR_1M_LENGTH)
                if atr_series is not None and len(atr_series) > 0:
                    val = atr_series.iloc[-1]
                    if not (isinstance(val, float) and math.isnan(val)):
                        return float(val)
            except Exception as e:
                logger.debug(f"SBS: 1m ATR fallback failed: {e}")

    return None


# ---------------------------------------------------------------------------
# RSI computation
# ---------------------------------------------------------------------------

def compute_rsi_5m(
    candles_by_tf: Dict[str, List[Dict]],
) -> Optional[float]:
    """
    Compute RSI(14) on 5m candles.

    Returns None if insufficient data.
    """
    candles_5m = candles_by_tf.get("5m", [])
    if len(candles_5m) < RSI_5M_LENGTH + 5:
        return None

    df = _candles_to_dataframe(candles_5m)
    if df is None or len(df) < RSI_5M_LENGTH + 5:
        return None

    try:
        rsi_series = ta.rsi(df["close"], length=RSI_5M_LENGTH)
        if rsi_series is not None and len(rsi_series) > 0:
            val = rsi_series.iloc[-1]
            if not (isinstance(val, float) and math.isnan(val)):
                return float(val)
    except Exception as e:
        logger.debug(f"SBS: 5m RSI failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------

def _detect_active_session(
    session_data: Optional[Dict[str, Any]],
    now_ts_ms: Optional[float] = None,
) -> Optional[str]:
    """
    Determine which session is currently active.

    Uses session_data if provided (from compute_sessions()).
    Falls back to timestamp-based detection if session_data is None.

    Returns the session key ("asian", "european", "us") or None.
    When multiple sessions overlap (EU+US), returns the one with higher weight.
    """
    if session_data:
        sessions = session_data.get("sessions", {})
        active = []
        for key, info in sessions.items():
            if info.get("active"):
                active.append(key)
        if not active:
            return None
        # Return highest weight active session
        # US > European > Asian
        for key in ["us", "european", "asian"]:
            if key in active:
                return key
        return active[0] if active else None

    # Fallback: use timestamp
    if now_ts_ms is None:
        now = datetime.datetime.now(datetime.timezone.utc)
        now_ts_ms = now.timestamp() * 1000.0

    utc_hour = _ts_to_utc_hour(now_ts_ms)

    for key in ["us", "european", "asian"]:
        cfg = SESSIONS[key]
        if cfg["open_utc"] <= utc_hour < cfg["close_utc"]:
            return key

    return None


def _compute_minutes_into_session(
    session_key: str,
    now_ts_ms: float,
) -> float:
    """
    Compute how many minutes have elapsed since session start.
    """
    today = _ts_to_utc_date(now_ts_ms)
    start_ts = _session_start_ts(session_key, today)
    elapsed_ms = now_ts_ms - start_ts
    return elapsed_ms / 60000.0


# ---------------------------------------------------------------------------
# Breakout detection
# ---------------------------------------------------------------------------

def _check_long_breakout(
    price: float,
    ib: Dict[str, Any],
    atr: Optional[float],
    cme_gap: Optional[float],
    rsi_5m: Optional[float],
    session_candles: List[Dict],
) -> tuple:
    """
    Check if conditions for a LONG breakout are met.

    Returns (is_breakout: bool, reasons: list).
    """
    reasons = []
    ib_high = ib["ib_high"]
    ib_low = ib["ib_low"]
    ib_range = ib["ib_range"]

    if not session_candles:
        return False, reasons

    # The latest candle's close
    last_close = session_candles[-1]["c"]
    last_open = session_candles[-1]["o"]
    last_body = abs(last_close - last_open)

    # 1. Price closes above IB_High
    if last_close <= ib_high:
        return False, reasons
    reasons.append("close>IB_H")

    # 2. Breakout candle body >= minimum
    if last_body < BREAKOUT_BODY_MIN:
        return False, reasons
    reasons.append(f"body≥{BREAKOUT_BODY_MIN}")

    # 3. RSI confirmation
    if rsi_5m is not None and rsi_5m <= RSI_LONG_THRESHOLD:
        return False, reasons
    if rsi_5m is not None:
        reasons.append(f"RSI>{RSI_LONG_THRESHOLD}")

    # 4. IB range filter (not too flat)
    if atr is not None and atr > 0:
        if ib_range < IB_RANGE_ATR_RATIO * atr:
            return False, reasons
        reasons.append("range≥0.3ATR")
    else:
        # If no ATR, skip this filter
        reasons.append("range_ok(no_ATR)")

    # 5. CME gap filter (negative gap = gap down = opposing long breakout)
    if cme_gap is not None and atr is not None and atr > 0:
        if cme_gap < 0 and abs(cme_gap) > CME_GAP_ATR_LIMIT * atr:
            return False, reasons
        reasons.append("gap_ok")
    else:
        reasons.append("gap_ok(no_data)")

    return True, reasons


def _check_short_breakout(
    price: float,
    ib: Dict[str, Any],
    atr: Optional[float],
    cme_gap: Optional[float],
    rsi_5m: Optional[float],
    session_candles: List[Dict],
) -> tuple:
    """
    Check if conditions for a SHORT breakout are met.

    Returns (is_breakout: bool, reasons: list).
    """
    reasons = []
    ib_high = ib["ib_high"]
    ib_low = ib["ib_low"]
    ib_range = ib["ib_range"]

    if not session_candles:
        return False, reasons

    # The latest candle's close
    last_close = session_candles[-1]["c"]
    last_open = session_candles[-1]["o"]
    last_body = abs(last_close - last_open)

    # 1. Price closes below IB_Low
    if last_close >= ib_low:
        return False, reasons
    reasons.append("close<IB_L")

    # 2. Breakout candle body >= minimum
    if last_body < BREAKOUT_BODY_MIN:
        return False, reasons
    reasons.append(f"body≥{BREAKOUT_BODY_MIN}")

    # 3. RSI confirmation
    if rsi_5m is not None and rsi_5m >= RSI_SHORT_THRESHOLD:
        return False, reasons
    if rsi_5m is not None:
        reasons.append(f"RSI<{RSI_SHORT_THRESHOLD}")

    # 4. IB range filter
    if atr is not None and atr > 0:
        if ib_range < IB_RANGE_ATR_RATIO * atr:
            return False, reasons
        reasons.append("range≥0.3ATR")
    else:
        reasons.append("range_ok(no_ATR)")

    # 5. CME gap filter (positive gap = gap up = opposing short breakout)
    if cme_gap is not None and atr is not None and atr > 0:
        if cme_gap > 0 and abs(cme_gap) > CME_GAP_ATR_LIMIT * atr:
            return False, reasons
        reasons.append("gap_ok")
    else:
        reasons.append("gap_ok(no_data)")

    return True, reasons


# ---------------------------------------------------------------------------
# Main: compute_session_breakout
# ---------------------------------------------------------------------------

def compute_session_breakout(
    candles_by_tf: Dict[str, List[Dict]],
    price: float,
    session_data: Optional[Dict[str, Any]] = None,
    atr_value: Optional[float] = None,
    rsi_value: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Compute the Session Breakout Strategy decision.

    Parameters
    ----------
    candles_by_tf : Dict[str, List[Dict]]
        Multi-timeframe candle data. Expected keys: "1m", "5m", "15m", "1h", "4h".
        Each dict has keys: t (open_time_ms), o, h, l, c, v.
    price : float
        Current price of the asset.
    session_data : dict, optional
        Session state from compute_sessions(). If None, detected from timestamps.
    atr_value : float, optional
        Pre-computed ATR value. If None, computed internally.
    rsi_value : float, optional
        Pre-computed RSI(14) on 5m. If None, computed internally.

    Returns
    -------
    dict : Standard strategy decision + sbs_data extras for frontend display.

        {
            "label": "BULLISH" | "BEARISH" | "LEAN BULL" | "LEAN BEAR" |
                     "NEUTRAL" | "IB" | "FADE" | "—",
            "color": "emerald" | "red" | "amber" | "gray",
            "bull_score": int,
            "bear_score": int,
            "rules_matched": list,
            "sbs_data": {
                "session": str | None,
                "session_label": str,
                "phase": "ib" | "breakout_window" | "post_window" | "no_session",
                "ib_high": float | None,
                "ib_low": float | None,
                "ib_range": float | None,
                "progressive_mid": float | None,
                "cme_gap": float | None,
                "breakout_direction": "long" | "short" | None,
                "tp1": float | None,
                "tp2": float | None,
                "sl": float | None,
                "minutes_into_session": float,
            }
        }
    """
    candles_1m = candles_by_tf.get("1m", [])

    # ── Default sbs_data ──
    sbs_data = {
        "session": None,
        "session_label": "",
        "phase": "no_session",
        "ib_high": None,
        "ib_low": None,
        "ib_range": None,
        "progressive_mid": None,
        "cme_gap": None,
        "breakout_direction": None,
        "tp1": None,
        "tp2": None,
        "sl": None,
        "minutes_into_session": 0.0,
    }

    # ── No 1m candles → no session data ──
    if not candles_1m:
        return {
            "label": "—",
            "color": "gray",
            "bull_score": 0,
            "bear_score": 0,
            "rules_matched": [],
            "sbs_data": sbs_data,
        }

    # ── Determine "now" from latest 1m candle timestamp ──
    now_ts_ms = candles_1m[-1]["t"] + 60000  # Candle open + 1m = approximate close time

    # ── Detect active session ──
    session_key = _detect_active_session(session_data, now_ts_ms)
    if not session_key:
        return {
            "label": "—",
            "color": "gray",
            "bull_score": 0,
            "bear_score": 0,
            "rules_matched": [],
            "sbs_data": sbs_data,
        }

    # ── Compute session-level metrics ──
    minutes_into = _compute_minutes_into_session(session_key, now_ts_ms)
    session_candles = get_session_candles(candles_1m, session_key, now_ts_ms)

    # Update sbs_data with session info
    sbs_data["session"] = session_key
    sbs_data["session_label"] = SESSIONS[session_key]["label"]
    sbs_data["minutes_into_session"] = round(minutes_into, 1)

    # ── Compute indicators ──
    atr = atr_value if atr_value is not None else compute_atr(candles_by_tf, candles_1m)
    rsi_5m = rsi_value if rsi_value is not None else compute_rsi_5m(candles_by_tf)
    cme_gap = compute_cme_gap(candles_1m, session_key, now_ts_ms)
    progressive_mid = compute_progressive_mid(session_candles) if session_candles else None

    sbs_data["cme_gap"] = round(cme_gap, 4) if cme_gap is not None else None
    sbs_data["progressive_mid"] = round(progressive_mid, 2) if progressive_mid is not None else None

    # ── Compute IB ──
    ib = compute_ib(candles_1m, session_key, now_ts_ms)

    if ib is None:
        # Session just started, no candles yet in the IB period
        sbs_data["phase"] = "ib"
        return {
            "label": "IB",
            "color": "gray",
            "bull_score": 0,
            "bear_score": 0,
            "rules_matched": ["session_starting"],
            "sbs_data": sbs_data,
        }

    sbs_data["ib_high"] = round(ib["ib_high"], 2)
    sbs_data["ib_low"] = round(ib["ib_low"], 2)
    sbs_data["ib_range"] = round(ib["ib_range"], 4) if ib["ib_range"] else 0.0

    ib_range = ib["ib_range"]

    # ── Phase 1: IB Period (0–30 min) ──
    if minutes_into < IB_DURATION_MIN:
        sbs_data["phase"] = "ib"
        return {
            "label": "IB",
            "color": "gray",
            "bull_score": 0,
            "bear_score": 0,
            "rules_matched": [
                f"IB_forming",
                f"IB_H={ib['ib_high']:.0f}",
                f"IB_L={ib['ib_low']:.0f}",
            ],
            "sbs_data": sbs_data,
        }

    # ── Phase 2: Breakout Window (30–120 min) ──
    if minutes_into < BREAKOUT_WINDOW_MIN:
        sbs_data["phase"] = "breakout_window"

        # Check LONG breakout
        long_break, long_reasons = _check_long_breakout(
            price, ib, atr, cme_gap, rsi_5m, session_candles
        )

        # Check SHORT breakout
        short_break, short_reasons = _check_short_breakout(
            price, ib, atr, cme_gap, rsi_5m, session_candles
        )

        if long_break and not short_break:
            # LONG breakout confirmed
            tp1 = ib["ib_high"] + ib_range * 1.0
            tp2 = ib["ib_high"] + ib_range * 2.0
            sl = ib["ib_low"] - ib_range * 0.1

            sbs_data["breakout_direction"] = "long"
            sbs_data["tp1"] = round(tp1, 2)
            sbs_data["tp2"] = round(tp2, 2)
            sbs_data["sl"] = round(sl, 2)

            return {
                "label": "BULLISH",
                "color": "emerald",
                "bull_score": 4,
                "bear_score": 0,
                "rules_matched": [f"LONG"] + long_reasons,
                "sbs_data": sbs_data,
            }

        if short_break and not long_break:
            # SHORT breakout confirmed
            tp1 = ib["ib_low"] - ib_range * 1.0
            tp2 = ib["ib_low"] - ib_range * 2.0
            sl = ib["ib_high"] + ib_range * 0.1

            sbs_data["breakout_direction"] = "short"
            sbs_data["tp1"] = round(tp1, 2)
            sbs_data["tp2"] = round(tp2, 2)
            sbs_data["sl"] = round(sl, 2)

            return {
                "label": "BEARISH",
                "color": "red",
                "bull_score": 0,
                "bear_score": 4,
                "rules_matched": [f"SHORT"] + short_reasons,
                "sbs_data": sbs_data,
            }

        # No breakout yet — show directional lean based on where price is relative to IB
        if price > ib["ib_mid"]:
            lean_rules = ["breakout_window", "price>IB_Mid", "awaiting_breakout"]
            if rsi_5m is not None:
                lean_rules.append(f"RSI={rsi_5m:.0f}")
            return {
                "label": "LEAN BULL",
                "color": "emerald",
                "bull_score": 1,
                "bear_score": 0,
                "rules_matched": lean_rules,
                "sbs_data": sbs_data,
            }
        elif price < ib["ib_mid"]:
            lean_rules = ["breakout_window", "price<IB_Mid", "awaiting_breakout"]
            if rsi_5m is not None:
                lean_rules.append(f"RSI={rsi_5m:.0f}")
            return {
                "label": "LEAN BEAR",
                "color": "red",
                "bull_score": 0,
                "bear_score": 1,
                "rules_matched": lean_rules,
                "sbs_data": sbs_data,
            }
        else:
            return {
                "label": "NEUTRAL",
                "color": "gray",
                "bull_score": 0,
                "bear_score": 0,
                "rules_matched": ["breakout_window", "price=IB_Mid", "awaiting_breakout"],
                "sbs_data": sbs_data,
            }

    # ── Phase 3: Post-Window (≥120 min) → FADE mode ──
    sbs_data["phase"] = "post_window"

    # Even after the window, check if a breakout already happened
    # (price is clearly outside IB range with momentum)
    if ib_range > 0:
        range_extension = 0  # How far price has moved beyond IB

        if price > ib["ib_high"]:
            range_extension = (price - ib["ib_high"]) / ib_range
        elif price < ib["ib_low"]:
            range_extension = (ib["ib_low"] - price) / ib_range

        # If price extended > 1.0 IB range beyond the boundary, consider it a
        # late breakout that already happened
        if range_extension > 1.0:
            if price > ib["ib_high"]:
                tp2 = ib["ib_high"] + ib_range * 2.0
                tp1 = ib["ib_high"] + ib_range * 1.0
                sl = ib["ib_low"] - ib_range * 0.1
                sbs_data["breakout_direction"] = "long"
                sbs_data["tp1"] = round(tp1, 2)
                sbs_data["tp2"] = round(tp2, 2)
                sbs_data["sl"] = round(sl, 2)

                # Score lower because it's a late/mature breakout
                return {
                    "label": "LEAN BULL",
                    "color": "emerald",
                    "bull_score": 2,
                    "bear_score": 0,
                    "rules_matched": [
                        "late_breakout_long",
                        f"extension={range_extension:.1f}R",
                    ],
                    "sbs_data": sbs_data,
                }
            else:
                tp2 = ib["ib_low"] - ib_range * 2.0
                tp1 = ib["ib_low"] - ib_range * 1.0
                sl = ib["ib_high"] + ib_range * 0.1
                sbs_data["breakout_direction"] = "short"
                sbs_data["tp1"] = round(tp1, 2)
                sbs_data["tp2"] = round(tp2, 2)
                sbs_data["sl"] = round(sl, 2)

                return {
                    "label": "LEAN BEAR",
                    "color": "red",
                    "bull_score": 0,
                    "bear_score": 2,
                    "rules_matched": [
                        "late_breakout_short",
                        f"extension={range_extension:.1f}R",
                    ],
                    "sbs_data": sbs_data,
                }

    # FADE mode: no breakout by IB + 90 min → contrarian
    # SELL at IB_High, BUY at IB_Low — informational, scored as NEUTRAL
    fade_rules = ["fade", "no_breakout_in_window"]
    if rsi_5m is not None:
        fade_rules.append(f"RSI={rsi_5m:.0f}")
    if cme_gap is not None:
        fade_rules.append(f"gap={'up' if cme_gap > 0 else 'down' if cme_gap < 0 else 'flat'}")

    return {
        "label": "FADE",
        "color": "amber",
        "bull_score": 0,
        "bear_score": 0,
        "rules_matched": fade_rules,
        "sbs_data": sbs_data,
    }

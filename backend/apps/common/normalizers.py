"""
Data Normalization Layer
========================
Broker-aware normalizers that transform raw WebSocket/REST data into a
canonical schema. All downstream code (intelligence engine, frontend)
consumes canonical data only — broker-specific field names never leak.

Architecture:
  - normalize_trade() / normalize_oi() dispatch on `broker` param
  - Each broker registers its field mapping in _BROKER_NORMALIZERS
  - Default (binance) maps are kept inline for zero-change backward compat
  - Adding Bybit: add entry to _BROKER_NORMALIZERS with its field mapping

Canonical Trade Schema:
    {
        "symbol": str,        # e.g. "BTC-251123-126000-C"
        "price": float,       # execution price
        "qty": float,         # quantity (always positive)
        "side": str,          # "BUY" or "SELL"
        "trade_type": str,    # "MARKET" or "BLOCK"
        "timestamp": int      # epoch ms
    }

Canonical OI Schema:
    {
        "symbol": str,             # e.g. "BTC-251123-126000-C"
        "oi_contracts": float,     # open interest in contracts
        "oi_usd": float,           # open interest in USDT
        "timestamp": int           # epoch ms
    }

Canonical Mark Price Schema (already consistent from WS, kept as-is).
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger("normalizers")


# ---------------------------------------------------------------------------
# Broker-specific field mappings
# ---------------------------------------------------------------------------
# Each entry defines how to extract canonical fields from raw broker data.
# Key = broker name (lowercase), value = field mapping dict.
#
# WS = WebSocket event fields
# REST = REST API response fields
#
# To add a new broker (e.g. Bybit):
#   _BROKER_NORMALIZERS["bybit"] = { ... }
#
# The normalize functions dispatch on this registry.
# ---------------------------------------------------------------------------

# Binance WS event field mapping (already canonical — identity mapping)
_BINANCE_WS_TRADE_MAP = {
    "symbol": "s",
    "price": "p",
    "qty": "q",
    "side": "S",
    "trade_type": "X",
    "timestamp": "T",
}

_BINANCE_WS_OI_MAP = {
    "symbol": "s",
    "oi_contracts": "o",
    "oi_usd": "h",
    "timestamp": "E",
}

_BROKER_NORMALIZERS: Dict[str, dict] = {
    "binance": {
        "ws_trade": _BINANCE_WS_TRADE_MAP,
        "ws_oi": _BINANCE_WS_OI_MAP,
    },
    # "bybit": { ... } — add when Bybit connector is implemented
}


def _get_normalizer_map(broker: str, data_type: str) -> Optional[dict]:
    """Look up the field mapping for a broker + data type. Returns None for unknown."""
    broker_config = _BROKER_NORMALIZERS.get(broker.lower())
    if not broker_config:
        return None
    return broker_config.get(data_type)


# ---------------------------------------------------------------------------
# Canonical field names — single source of truth
# ---------------------------------------------------------------------------
CANONICAL_TRADE = {
    "symbol": "s",
    "price": "p",
    "qty": "q",
    "side": "S",
    "trade_type": "X",
    "timestamp": "T"
}
CANONICAL_OI = {
    "symbol": "s",
    "oi_contracts": "oi_contracts",
    "oi_usd": "oi_usd",
    "timestamp": "ts"
}


def normalize_trade(raw: dict, source: str = "ws", broker: str = None) -> dict:
    """
    Normalize a single trade dict to the canonical trade schema.

    Dispatches on `broker` parameter for broker-specific WS field mappings.
    REST normalization uses fixed logic per source format.

    Binance WS @optionTrade fields:
        s (symbol), p (price), q (qty, always positive),
        S ("BUY"/"SELL"), X ("MARKET"/"BLOCK"), T (epoch ms)

    Binance REST /eapi/v1/blockTrades fields:
        symbol, price, qty (can be negative),
        side (-1/1 int), time (epoch ms)

    Future brokers (Bybit etc.):
        Add entry to _BROKER_NORMALIZERS["bybit"]["ws_trade"]
    """
    try:
        if source == "rest":
            # REST block trades: side is -1 (sell) or 1 (buy)
            side_int = int(raw.get("side", 0))
            qty_val = abs(float(raw.get("qty", 0)))
            return {
                "symbol": raw.get("symbol", ""),
                "price": float(raw.get("price", 0)),
                "qty": qty_val,
                "side": "BUY" if side_int >= 0 else "SELL",
                "trade_type": "BLOCK",
                "timestamp": int(raw.get("time", 0)),
            }

        # WS trade — dispatch by broker
        if broker and broker.lower() != "binance":
            norm_map = _get_normalizer_map(broker, "ws_trade")
            if norm_map:
                return _normalize_with_map(raw, norm_map)

        # Default: Binance WS optionTrade — fields already match canonical naming
        return {
            "symbol": raw.get("s", ""),
            "price": float(raw.get("p", 0)),
            "qty": float(raw.get("q", 0)),  # always positive per docs
            "side": raw.get("S", "BUY"),     # "BUY" or "SELL"
            "trade_type": raw.get("X", "MARKET"),
            "timestamp": int(raw.get("T", raw.get("E", 0))),
        }
    except (ValueError, TypeError) as e:
        logger.warning(f"normalize_trade failed for {raw.get('symbol', '?')}: {e}")
        return None


def normalize_oi(raw: dict, source: str = "ws", broker: str = None) -> dict:
    """
    Normalize a single OI dict to the canonical OI schema.

    Dispatches on `broker` parameter for broker-specific WS field mappings.

    Binance WS openInterest fields:
        s (symbol), o (OI in contracts), h (OI in USDT), E (event time)

    Binance REST /eapi/v1/openInterest fields:
        symbol, sumOpenInterest, sumOpenInterestUsd, timestamp
    """
    try:
        if source == "rest":
            return {
                "symbol": raw.get("symbol", ""),
                "oi_contracts": float(raw.get("sumOpenInterest", 0)),
                "oi_usd": float(raw.get("sumOpenInterestUsd", 0)),
                "timestamp": int(raw.get("timestamp", 0)),
            }

        # WS OI — dispatch by broker
        if broker and broker.lower() != "binance":
            norm_map = _get_normalizer_map(broker, "ws_oi")
            if norm_map:
                return _normalize_with_map(raw, norm_map)

        # Default: Binance WS openInterest
        return {
            "symbol": raw.get("s", ""),
            "oi_contracts": float(raw.get("o", 0)),
            "oi_usd": float(raw.get("h", 0)),
            "timestamp": int(raw.get("E", 0)),
        }
    except (ValueError, TypeError) as e:
        logger.warning(f"normalize_oi failed for {raw.get('symbol', '?')}: {e}")
        return None


def normalize_oi_list(raw_list: list, source: str = "ws", broker: str = None) -> list:
    """Normalize a list of OI dicts, filtering out failures."""
    results = []
    for item in raw_list:
        normalized = normalize_oi(item, source=source, broker=broker)
        if normalized:
            results.append(normalized)
    return results


def normalize_trade_list(raw_list: list, source: str = "ws", broker: str = None) -> list:
    """Normalize a list of trade dicts, filtering out failures."""
    results = []
    for item in raw_list:
        normalized = normalize_trade(item, source=source, broker=broker)
        if normalized:
            results.append(normalized)
    return results


# ---------------------------------------------------------------------------
# Generic map-based normalizer (for future brokers)
# ---------------------------------------------------------------------------

def _normalize_with_map(raw: dict, field_map: dict) -> dict:
    """
    Generic normalizer that extracts canonical fields from raw data
    using a field mapping dict.

    field_map format: { canonical_name: raw_field_name }
    Example: {"symbol": "s", "price": "p", ...}
    """
    result = {}
    for canonical_name, raw_field in field_map.items():
        val = raw.get(raw_field)
        if val is not None:
            result[canonical_name] = val
    return result
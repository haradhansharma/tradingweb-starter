# backend/apps/common/normalizers.py

"""
Data Normalization Layer
========================
Binance WebSocket and REST APIs return data with different field names for the same concepts.
This module provides canonical normalizers so ALL downstream code uses ONE consistent schema.

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

logger = logging.getLogger("normalizers")


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


def normalize_trade(raw: dict, source: str = "ws") -> dict:
    """
    Normalize a single trade dict to the canonical trade schema.

    WS @optionTrade fields:
        s (symbol), p (price), q (qty, always positive),
        S ("BUY"/"SELL"), X ("MARKET"/"BLOCK"), T (epoch ms)

    REST /eapi/v1/blockTrades fields:
        symbol, price, qty (can be negative),
        side (-1/1 int), time (epoch ms)
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
        else:
            # WS optionTrade — fields already match canonical naming
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


def normalize_oi(raw: dict, source: str = "ws") -> dict:
    """
    Normalize a single OI dict to the canonical OI schema.

    WS openInterest fields:
        s (symbol), o (OI in contracts), h (OI in USDT), E (event time)

    REST /eapi/v1/openInterest fields:
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
        else:
            return {
                "symbol": raw.get("s", ""),
                "oi_contracts": float(raw.get("o", 0)),
                "oi_usd": float(raw.get("h", 0)),
                "timestamp": int(raw.get("E", 0)),
            }
    except (ValueError, TypeError) as e:
        logger.warning(f"normalize_oi failed for {raw.get('symbol', '?')}: {e}")
        return None


def normalize_oi_list(raw_list: list, source: str = "ws") -> list:
    """Normalize a list of OI dicts, filtering out failures."""
    results = []
    for item in raw_list:
        normalized = normalize_oi(item, source=source)
        if normalized:
            results.append(normalized)
    return results


def normalize_trade_list(raw_list: list, source: str = "ws") -> list:
    """Normalize a list of trade dicts, filtering out failures."""
    results = []
    for item in raw_list:
        normalized = normalize_trade(item, source=source)
        if normalized:
            results.append(normalized)
    return results

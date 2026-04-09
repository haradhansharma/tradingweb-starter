"""
Broker Configuration Registry
=============================
Single source of truth for broker-specific settings.
Every broker (Binance, Bybit, etc.) registers its configuration here.

Adding a new broker:
  1. Add entry to BROKER_CONFIGS
  2. Create {broker}_connector.py inheriting from the appropriate base
  3. Create {broker}_rest.py for REST API calls
  4. Register sync tasks in beat schedule
  5. That's it — all Redis keys, group names, API endpoints auto-adapt

No calculation engine code needs to change — they receive canonical data
regardless of broker source.
"""

from typing import Dict, List, Optional
from django.conf import settings


# ---------------------------------------------------------------------------
# Default broker
# ---------------------------------------------------------------------------
DEFAULT_BROKER = "binance"


# ---------------------------------------------------------------------------
# Broker Configuration Registry
# ---------------------------------------------------------------------------
BROKER_CONFIGS: Dict[str, dict] = {
    "binance": {
        "display_name": "Binance",
        "redis_prefix": "binance",

        # Options WebSocket URLs
        "options": {
            "ws_market_base": "wss://fstream.binance.com/market",
            "ws_public_base": "wss://fstream.binance.com/public",
            "rest_base_url": "https://eapi.binance.com",
            "rest_base_url_testnet": "https://testnet.binancefuture.com",
            "ws_market_base_testnet": "wss://fstream.binancefuture.com/market",
            "ws_public_base_testnet": "wss://fstream.binancefuture.com/public",
            "api_key_setting": "BINANCE_API_KEY",
            "api_secret_setting": "BINANCE_API_SECRET",
            "exchange_info_path": "/eapi/v1/exchangeInfo",
            "open_interest_path": "/eapi/v1/openInterest",
            "ticker_path": "/eapi/v1/ticker",
            "trades_path": "/eapi/v1/trades",
            "block_trades_path": "/eapi/v1/blockTrades",
            "account_path": "/eapi/v1/account",
            "auth_header": "X-MBX-APIKEY",
        },

        # Futures (USDM) settings
        "futures": {
            "ws_base_url": "wss://fstream.binance.com/market",
            "ws_base_url_testnet": "wss://fstream.binancefuture.com/market",
            "rest_base_url": "https://fapi.binance.com",
            "rest_base_url_testnet": "https://testnet.binancefuture.com",
            "kline_path": "/fapi/v1/klines",
            "exchange_info_path": "/fapi/v1/exchangeInfo",
        },

        # Rate limits for REST API calls
        "rate_limits": {
            "requests_per_second": 50,
            "semaphore_limit": 5,
            "inter_request_delay": 0.1,
        },

        # Error codes that should NOT be retried
        "non_retryable_codes": {418, 429},

        # Supported kline intervals (from broker docs)
        "valid_intervals": {
            "1m", "3m", "5m", "15m", "30m",
            "1h", "2h", "4h", "6h", "8h", "12h",
            "1d", "3d", "1w", "1M",
        },

        # Redis key prefixes for this broker
        "redis_keys": {
            "exchange_info": "exchange_info",
            "active_underlyings": "active_underlyings",
            "open_interest": "open_interest",
            "mark_price": "markPrice",
            "index": "index",
            "ticker": "ticker",
            "trade": "trade",
            "intelligence": "intelligence",
        },

        # Normalizer identifiers (maps to normalizers.py functions)
        "normalizer": "binance",

        # Max kline candles per REST request
        "max_kline_limit": 1500,
    },

    # ── Future brokers (placeholder) ──
    # "bybit": {
    #     "display_name": "Bybit",
    #     "redis_prefix": "bybit",
    #     "options": {
    #         "ws_base_url": "wss://stream.bybit.com/v5/public/option",
    #         "rest_base_url": "https://api.bybit.com",
    #         ...
    #     },
    #     "futures": { ... },
    #     "redis_keys": { ... },
    #     "normalizer": "bybit",
    # },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_broker_config(broker: str = DEFAULT_BROKER) -> dict:
    """
    Get configuration for a specific broker.
    Raises KeyError if broker is not registered.
    """
    broker = broker.lower()
    if broker not in BROKER_CONFIGS:
        available = ", ".join(BROKER_CONFIGS.keys())
        raise KeyError(
            f"Unknown broker '{broker}'. Available: {available}"
        )
    return BROKER_CONFIGS[broker]


def get_redis_prefix(broker: str = DEFAULT_BROKER) -> str:
    """Get the Redis key prefix for a broker (e.g., 'binance')."""
    return get_broker_config(broker)["redis_prefix"]


def redis_key(broker: str, underlying: str, category: str) -> str:
    """
    Build a broker-namespaced Redis key.

    Examples:
        redis_key("binance", "BTCUSDT", "open_interest") → "binance:BTCUSDT:open_interest"
        redis_key("bybit", "BTCUSDT", "markPrice")      → "bybit:BTCUSDT:markPrice"
    """
    prefix = get_redis_prefix(broker)
    return f"{prefix}:{underlying}:{category}"


def redis_global_key(broker: str, key: str) -> str:
    """
    Build a broker-namespaced global Redis key (no underlying).

    Examples:
        redis_global_key("binance", "exchange_info")      → "binance:exchange_info"
        redis_global_key("binance", "active_underlyings") → "binance:active_underlyings"
    """
    prefix = get_redis_prefix(broker)
    return f"{prefix}:{key}"


# def ws_group_name(broker: str, underlying: str, category: str) -> str:
#     """
#     Build a broker-namespaced Django Channels group name.

#     Examples:
#         ws_group_name("binance", "BTCUSDT", "intelligence") → "binance:BTCUSDT_intelligence"
#         ws_group_name("binance", "BTCUSDT", "indicators")   → "binance:BTCUSDT_indicators"
#     """
#     prefix = get_redis_prefix(broker)
#     return f"{prefix}:{underlying}_{category}".replace("-", "_")


# def ws_global_group(broker: str, category: str) -> str:
#     """
#     Build a broker-namespaced global Django Channels group name.

#     Examples:
#         ws_global_group("binance", "sessions") → "binance:_global_sessions"
#     """
#     prefix = get_redis_prefix(broker)
#     return f"{prefix}:_global_{category}"

def ws_group_name(broker: str, underlying: str, category: str) -> str:
    """
    Build a Django Channels group name for per-symbol subscriptions.

    Uses dots instead of colons because Channels group names cannot
    contain colons (they conflict with ASGI scope keys).

    Example:  ws_group_name("binance", "BTCUSDT", "markPrice")
              → "binance.BTCUSDT.markPrice"
    """
    return f"{broker}.{underlying.upper()}.{category}"


def ws_global_group(broker: str, category: str) -> str:
    """
    Build a Django Channels group name for global (non-per-symbol) channels.

    Example:  ws_global_group("binance", "sessions")
              → "binance_GLOBAL.sessions"
    """
    return f"{broker}_GLOBAL.{category}"


def pubsub_channel(broker: str, underlying: str, category: str) -> str:
    """
    Build a broker-namespaced Redis Pub/Sub channel name.

    Examples:
        pubsub_channel("binance", "BTCUSDT", "markPrice") → "binance:signal:BTCUSDT:markPrice"
    """
    prefix = get_redis_prefix(broker)
    return f"{prefix}:signal:{underlying}:{category}"


def get_available_brokers() -> List[Dict[str, str]]:
    """
    Get list of available brokers for the frontend selector.

    Returns:
        [{"id": "binance", "display_name": "Binance"}, ...]
    """
    return [
        {"id": broker_id, "display_name": config["display_name"]}
        for broker_id, config in BROKER_CONFIGS.items()
    ]


def get_broker_settings(broker: str = DEFAULT_BROKER) -> dict:
    """
    Get broker-specific Django settings (API keys, etc.).

    Returns dict with at least 'api_key' and 'api_secret'.
    """
    config = get_broker_config(broker)
    options = config.get("options", {})
    key_setting = options.get("api_key_setting", "")
    secret_setting = options.get("api_secret_setting", "")

    return {
        "api_key": getattr(settings, key_setting, "") if key_setting else "",
        "api_secret": getattr(settings, secret_setting, "") if secret_setting else "",
        "auth_header": options.get("auth_header", ""),
    }


def get_rate_limits(broker: str = DEFAULT_BROKER) -> dict:
    """
    Get broker-specific rate limits for REST API calls.
    Falls back to sensible defaults if not configured.
    """
    config = get_broker_config(broker)
    return config.get("rate_limits", {
        "requests_per_second": 50,
        "semaphore_limit": 5,
        "inter_request_delay": 0.1,
    })


def get_non_retryable_codes(broker: str = DEFAULT_BROKER) -> set:
    """
    Get broker-specific HTTP error codes that should NOT be retried.
    """
    config = get_broker_config(broker)
    return config.get("non_retryable_codes", {418, 429})


def get_valid_intervals(broker: str = DEFAULT_BROKER) -> set:
    """
    Get broker-specific supported kline intervals.
    """
    config = get_broker_config(broker)
    return config.get("valid_intervals", {
        "1m", "3m", "5m", "15m", "30m",
        "1h", "2h", "4h", "6h", "8h", "12h",
        "1d", "3d", "1w", "1M",
    })

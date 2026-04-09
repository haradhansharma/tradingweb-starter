"""
Centralized Asset Registry
===========================
Single source of truth for active trading underlyings.
Both options and futures pipelines derive their symbol lists from
`{broker}:active_underlyings` (populated by `sync_exchange_structure`
from Binance Options exchange info — ranked by number of active strikes).

All consumers MUST read through these functions. Never hardcode
["BTCUSDT", "ETHUSDT"] inline — the single DEFAULT_FALLBACK here
is the only exception.
"""

import json
import logging
from typing import List

logger = logging.getLogger("assets")

DEFAULT_FALLBACK = ["BTCUSDT", "ETHUSDT"]


def _redis_key(broker: str = "binance") -> str:
    """Build Redis key for active underlyings for a given broker."""
    return f"{broker}:active_underlyings"


def get_assets_sync(redis_client, broker: str = "binance") -> List[str]:
    """
    Get active underlyings from Redis (synchronous).
    Used by Celery tasks and Django management commands.
    """
    key = _redis_key(broker)
    raw = redis_client.get(key)
    if raw:
        assets = json.loads(raw)
        logger.debug(f"Loaded {len(assets)} assets from Redis key '{key}'")
        return assets
    logger.warning(
        f"Redis key '{key}' not found. Using fallback: {DEFAULT_FALLBACK}"
    )
    return list(DEFAULT_FALLBACK)


async def get_assets_async(redis_client, broker: str = "binance") -> List[str]:
    """
    Get active underlyings from Redis (async).
    Used by API endpoints and async contexts.
    """
    key = _redis_key(broker)
    raw = await redis_client.get(key)
    if raw:
        assets = json.loads(raw)
        logger.debug(f"Loaded {len(assets)} assets from Redis key '{key}'")
        return assets
    logger.warning(
        f"Redis key '{key}' not found. Using fallback: {DEFAULT_FALLBACK}"
    )
    return list(DEFAULT_FALLBACK)

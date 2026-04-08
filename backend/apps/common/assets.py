"""
Centralized Asset Registry
===========================
Single source of truth for active trading underlyings.
Both options and futures pipelines derive their symbol lists from
`binance:active_underlyings` (populated by `sync_exchange_structure`
from Binance Options exchange info — ranked by number of active strikes).

All consumers MUST read through these functions. Never hardcode
["BTCUSDT", "ETHUSDT"] inline — the single DEFAULT_FALLBACK here
is the only exception.
"""

import json
import logging
from typing import List

logger = logging.getLogger("assets")

REDIS_KEY = "binance:active_underlyings"
DEFAULT_FALLBACK = ["BTCUSDT", "ETHUSDT"]


def get_assets_sync(redis_client) -> List[str]:
    """
    Get active underlyings from Redis (synchronous).
    Used by Celery tasks and Django management commands.
    """
    raw = redis_client.get(REDIS_KEY)
    if raw:
        assets = json.loads(raw)
        logger.debug(f"Loaded {len(assets)} assets from Redis key '{REDIS_KEY}'")
        return assets
    logger.warning(
        f"Redis key '{REDIS_KEY}' not found. Using fallback: {DEFAULT_FALLBACK}"
    )
    return list(DEFAULT_FALLBACK)


async def get_assets_async(redis_client) -> List[str]:
    """
    Get active underlyings from Redis (async).
    Used by API endpoints and async contexts.
    """
    raw = await redis_client.get(REDIS_KEY)
    if raw:
        assets = json.loads(raw)
        logger.debug(f"Loaded {len(assets)} assets from Redis key '{REDIS_KEY}'")
        return assets
    logger.warning(
        f"Redis key '{REDIS_KEY}' not found. Using fallback: {DEFAULT_FALLBACK}"
    )
    return list(DEFAULT_FALLBACK)

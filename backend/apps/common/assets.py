"""
Centralized Asset Registry
===========================
Single source of truth for active trading underlyings.
Both options and futures pipelines derive their symbol lists from
`{broker}:active_underlyings` (populated by `sync_exchange_structure`
from exchange info — ranked by number of active strikes).

All consumers MUST read through these functions. Never hardcode
["BTCUSDT", "ETHUSDT"] inline — the single DEFAULT_FALLBACK here
is the only exception.
"""

import json
import logging
from typing import List

from .broker_config import redis_global_key, DEFAULT_BROKER

logger = logging.getLogger("assets")

DEFAULT_FALLBACK = ["BTCUSDT", "ETHUSDT"]


def get_assets_sync(redis_client, broker: str = None) -> List[str]:
    """
    Get active underlyings from Redis (synchronous).
    Used by Celery tasks and Django management commands.
    """
    broker = broker or DEFAULT_BROKER
    key = redis_global_key(broker, "active_underlyings")
    raw = redis_client.get(key)
    if raw:
        assets = json.loads(raw)
        logger.debug(f"Loaded {len(assets)} assets from Redis key '{key}'")
        return assets
    logger.warning(
        f"Redis key '{key}' not found. Using fallback: {DEFAULT_FALLBACK}"
    )
    return list(DEFAULT_FALLBACK)


async def get_assets_async(redis_client, broker: str = None) -> List[str]:
    """
    Get active underlyings from Redis (async).
    Used by API endpoints and async contexts.
    """
    broker = broker or DEFAULT_BROKER
    key = redis_global_key(broker, "active_underlyings")
    raw = await redis_client.get(key)
    if raw:
        assets = json.loads(raw)
        logger.debug(f"Loaded {len(assets)} assets from Redis key '{key}'")
        return assets
    logger.warning(
        f"Redis key '{key}' not found. Using fallback: {DEFAULT_FALLBACK}"
    )
    return list(DEFAULT_FALLBACK)

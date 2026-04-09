"""
Redis Bridge
============
Bridges broker WebSocket data → Redis cache → Django Channel Layer → Frontend.

Broker-agnostic: accepts a `broker` parameter to namespace all Redis keys,
channel groups, and pub/sub channels. Adding a new broker only requires
registering it in broker_config.py — no changes to this file.

Responsibilities:
  1. Cache normalized market data in Redis (canonical schema only).
  2. Publish raw payloads to Redis Pub/Sub for external consumers.
  3. Broadcast to Django Channel Layer groups for WebSocket consumers.
  4. Trigger intelligence calculations on mark price updates (throttled).
  5. Track data freshness and detect stale metrics.
  6. Broadcast error notifications to connected clients.
"""

import json
import time
import logging
from typing import Any, Dict, List, Optional

from django.conf import settings
from channels.layers import get_channel_layer
import redis.asyncio as aioredis
import asyncio

from .intelligence_engine import OptionIntelligenceEngine
from .normalizers import normalize_oi_list, normalize_trade_list
from .broker_config import (
    redis_key,
    redis_global_key,
    ws_group_name,
    pubsub_channel,
    DEFAULT_BROKER,
)

logger = logging.getLogger("bridge")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CATEGORY_OPEN_INTEREST = "open_interest"  # canonical snake_case
CATEGORY_MARK_PRICE = "markPrice"
CATEGORY_INDEX = "index"
CATEGORY_TICKER = "ticker"
CATEGORY_TRADE = "trade"
CATEGORY_INTELLIGENCE = "intelligence"
CATEGORY_META = "meta"
CATEGORY_ERROR = "error"

# Throttle: max one intelligence calc per underlying per this many seconds
INTELLIGENCE_THROTTLE_SEC = 2.0

# Stale threshold in seconds — metrics older than this are flagged
STALE_THRESHOLD_SEC = 300  # 5 minutes

# Cleanup interval for throttle cache (seconds)
THROTTLE_CLEANUP_INTERVAL = 3600  # 1 hour

# Snapshot persistence interval (seconds) — save to DB every N seconds per asset
SNAPSHOT_INTERVAL_SEC = 60


class RedisBridge:
    def __init__(self, broker: str = DEFAULT_BROKER):
        self.broker = broker
        self.cache = aioredis.from_url(settings.CACHES["default"]["LOCATION"])
        self.pubsub = aioredis.from_url(settings.REDIS_PUBSUB_URL)
        self.channel_layer = get_channel_layer()
        self.engine = OptionIntelligenceEngine()
        self.last_intel_time: Dict[str, float] = {}  # {underlying: timestamp}
        self.last_snapshot_time: Dict[str, float] = {}  # {underlying: timestamp}
        self.last_throttle_cleanup = time.time()

    async def close(self):
        """Call this when the streamer stops to release connections."""
        try:
            await self.cache.aclose()
        except Exception as e:
            logger.warning(f"Error closing cache connection: {e}")
        try:
            await self.pubsub.aclose()
        except Exception as e:
            logger.warning(f"Error closing pubsub connection: {e}")

    # ------------------------------------------------------------------
    # Public: broadcast
    # ------------------------------------------------------------------

    async def broadcast(
        self,
        category: str,
        underlying: str,
        data: Any,
        stream_name: str = None,
    ):
        """
        Cache, publish, and broadcast market data.
        Normalizes data at write time so Redis always contains canonical schema.
        """
        try:
            # --- Normalize data BEFORE caching (Phase 1) ---
            normalized_data = self._normalize_for_storage(category, data)

            payload = {
                "broker": self.broker,
                "category": category,
                "underlying": underlying,
                "data": normalized_data,
                "stream": stream_name,
            }
            raw_payload = json.dumps(payload)
            cache_key = redis_key(self.broker, underlying, category)
            group_name = ws_group_name(self.broker, underlying, category)

            tasks = []

            # Cache: OI and Trade are managed EXCLUSIVELY by REST sync (tasks.py).
            #
            # OI (Issue #2): WS OI events are incremental (single-contract changes)
            # and must NOT overwrite the full REST snapshot — doing so would corrupt
            # PCR, Max Pain, Walls, and GEX calculations.
            #
            # Trade (Issue #3): WS trade events are overwhelmingly MARKET trades
            # (non-BLOCK). The REST sync seeds BLOCK trades specifically. If WS trades
            # overwrite the cache, the engine's whale detection (weight=40, the
            # heaviest signal) sees zero block trades and permanently dies.
            #
            # All other categories (markPrice, index, ticker) are safe to cache from WS.
            if category not in (CATEGORY_OPEN_INTEREST, CATEGORY_TRADE):
                tasks.append(
                    self.cache.set(cache_key, raw_payload, ex=self._cache_ttl(category))
                )

            # Pub/Sub (raw for external consumers)
            tasks.append(
                self.pubsub.publish(
                    pubsub_channel(self.broker, underlying, category), raw_payload
                )
            )

            # Channel Layer group send
            tasks.append(
                self.channel_layer.group_send(
                    group_name,
                    {"type": "market.update", "payload": payload},
                )
            )

            # --- INTELLIGENCE TRIGGER ---
            now = time.time()
            if category == CATEGORY_MARK_PRICE:
                if (
                    now - self.last_intel_time.get(underlying, 0)
                ) > INTELLIGENCE_THROTTLE_SEC:
                    self.last_intel_time[underlying] = now
                    self._maybe_cleanup_throttle(now)
                    intel_tasks = await self._generate_intelligence(
                        underlying, normalized_data
                    )
                    if intel_tasks:
                        tasks.extend(intel_tasks)

            await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"BRIDGE ERROR [{category}/{underlying}]: {e}", exc_info=True)
            # Broadcast error to subscribed clients
            await self._broadcast_error(underlying, str(e), category)

    # ------------------------------------------------------------------
    # Normalization at Write Time
    # ------------------------------------------------------------------

    def _normalize_for_storage(self, category: str, data: Any) -> Any:
        """
        Normalize data to canonical schema before writing to Redis.
        This ensures downstream readers ALWAYS see the same field names,
        regardless of whether data came from WS or REST.
        """
        if not isinstance(data, list):
            return data

        if category == CATEGORY_TRADE:
            # Detect source: if items have 'q' field → WS, if 'qty' → REST
            if data and isinstance(data[0], dict):
                source = "ws" if "q" in data[0] else "rest"
                return normalize_trade_list(data, source=source, broker=self.broker)

        elif category == CATEGORY_OPEN_INTEREST:
            if data and isinstance(data[0], dict):
                # WS has 'o' field, REST has 'sumOpenInterest'
                source = "ws" if "o" in data[0] else "rest"
                return normalize_oi_list(data, source=source, broker=self.broker)

        # markPrice, index, ticker — WS data is stored as-is (field names are final)
        return data

    def _cache_ttl(self, category: str) -> int:
        """Category-specific cache TTLs."""
        ttl_map = {
            CATEGORY_MARK_PRICE: 60,
            CATEGORY_INDEX: 60,
            CATEGORY_TICKER: 60,
            CATEGORY_OPEN_INTEREST: 1800,
            CATEGORY_TRADE: 3600,
            CATEGORY_INTELLIGENCE: 60,
        }
        return ttl_map.get(category, 120)

    # ------------------------------------------------------------------
    # Intelligence Generation
    # ------------------------------------------------------------------

    async def _generate_intelligence(
        self, asset: str, mark_chunk: list
    ) -> Optional[List]:
        """
        Aggregate state for the specific asset and trigger intelligence calculation.
        Returns list of asyncio tasks for cache write + channel broadcast, or None.
        """
        try:
            asset = asset.upper()
            keys = [
                redis_key(self.broker, asset, CATEGORY_INDEX),
                redis_key(self.broker, asset, CATEGORY_OPEN_INTEREST),
                redis_key(self.broker, asset, CATEGORY_TRADE),
            ]
            raw_vals = await self.cache.mget(keys)
            now = time.time()

            def extract_with_timestamp(raw_json: bytes, category: str) -> tuple:
                """Extract data and compute age in ms. Returns None for unknown age."""
                if not raw_json:
                    return None, None
                parsed = json.loads(raw_json)
                data = (
                    parsed.get("data")
                    if isinstance(parsed, dict) and "data" in parsed
                    else parsed
                )
                # Data freshness: use event timestamp if available, else use cache write time
                age_ms = self._estimate_data_age(data, category, now)
                return data, age_ms

            index_payload, index_age = extract_with_timestamp(
                raw_vals[0], CATEGORY_INDEX
            )
            oi_payload, oi_age = extract_with_timestamp(
                raw_vals[1], CATEGORY_OPEN_INTEREST
            )
            trade_payload, trade_age = extract_with_timestamp(
                raw_vals[2], CATEGORY_TRADE
            )

            data_timestamps = {
                "mark_price_age_ms": 0,  # mark_chunk is live
                "index_age_ms": round(index_age, 0) if index_age is not None else None,
                "open_interest_age_ms": round(oi_age, 0) if oi_age is not None else None,
                "trade_age_ms": round(trade_age, 0) if trade_age is not None else None,
            }

            # --- Guard: skip if OI or index missing from cache ---
            # OI cache is managed EXCLUSIVELY by REST sync (tasks.py).
            # WS OI events no longer write to cache to prevent incremental data
            # from overwriting the authoritative REST snapshot (Issue #2).
            #
            # If OI/index is missing here, it means REST sync hasn't populated
            # the cache yet (fresh start) or the key expired. Rather than
            # broadcasting `insufficient_data` (which triggers _removeAsset()
            # on the frontend), we skip silently. The asset stays CONNECTING
            # and the 15s CONNECTING timeout is the proper safety net for
            # permanently-missing data.
            if not index_payload or not oi_payload:
                missing = []
                if not index_payload:
                    missing.append("index")
                if not oi_payload:
                    missing.append("open_interest")
                logger.info(
                    f"Skipping intelligence for {asset}: "
                    f"missing {missing} — waiting for REST sync to populate cache"
                )
                return None  # Skip silently — don't broadcast insufficient_data

            # Handle both WS ('p') and REST ('indexPrice') formats for index
            index_price = index_payload.get("p") or index_payload.get("indexPrice")
            if not index_price:
                logger.info(
                    f"Skipping intelligence for {asset}: "
                    f"index has no price field — waiting for next tick"
                )
                return None  # Skip silently

            # Build contract size lookup from exchangeInfo.
            # Binance European Options have a 'contractSize' per symbol (1 for BTC/ETH).
            # This is needed for accurate notional (Whale) and GEX calculations.
            contract_sizes = {}
            try:
                ext_raw = await self.cache.get(redis_global_key(self.broker, "exchange_info"))
                if ext_raw:
                    ext_info = json.loads(ext_raw)
                    for sym_info in ext_info.get("optionSymbols", []):
                        sym_name = sym_info.get("symbol", "")
                        try:
                            contract_sizes[sym_name] = float(
                                sym_info.get("contractSize", 1)
                            )
                        except (ValueError, TypeError):
                            contract_sizes[sym_name] = 1.0
            except Exception as e:
                logger.debug(f"Contract sizes unavailable for {asset}: {e}")

            intel_report = self.engine.run_analysis(
                asset=asset,
                index_price=float(index_price),
                mark_data=mark_chunk,
                oi_data=oi_payload if isinstance(oi_payload, list) else [],
                trade_data=trade_payload if isinstance(trade_payload, list) else [],
                data_timestamps=data_timestamps,
                contract_sizes=contract_sizes,
            )

            intel_payload = {
                "broker": self.broker,
                "category": CATEGORY_INTELLIGENCE,
                "underlying": asset,
                "data": intel_report,
            }
            intel_raw = json.dumps(intel_payload)
            intel_group = ws_group_name(self.broker, asset, CATEGORY_INTELLIGENCE)

            # --- A6: Persist snapshot to DB (throttled, async) ---
            # Lazy import to avoid circular dependency (bridge ↔ tasks)
            last_snap = self.last_snapshot_time.get(asset, 0)
            if now - last_snap >= SNAPSHOT_INTERVAL_SEC:
                self.last_snapshot_time[asset] = now
                try:
                    from .tasks import _base_save_intelligence_snapshot
                    _base_save_intelligence_snapshot(self.broker, asset, intel_report)
                except Exception as snap_err:
                    logger.warning(
                        f"Snapshot enqueue failed for {asset}: {snap_err}"
                    )

            return [
                self.cache.set(
                    redis_key(self.broker, asset, CATEGORY_INTELLIGENCE), intel_raw, ex=60
                ),
                self.channel_layer.group_send(
                    intel_group,
                    {"type": "market.update", "payload": intel_payload},
                ),
            ]

        except Exception as e:
            logger.error(f"INTELLIGENCE ERROR [{asset}]: {e}", exc_info=True)
            return None

    def _build_insufficient_response(
        self, asset: str, missing: list, data_timestamps: dict
    ) -> list:
        """
        Build a WS response for assets with insufficient data.
        Sends a status payload so the frontend can display an appropriate
        state instead of staying frozen on 'CONNECTING'.
        """
        status_data = {
            "status": "insufficient_data",
            "asset": asset,
            "missing": missing,
            "stale": missing,
            "data_timestamps": data_timestamps,
        }
        intel_payload = {
            "broker": self.broker,
            "category": CATEGORY_INTELLIGENCE,
            "underlying": asset,
            "data": status_data,
        }
        intel_group = ws_group_name(self.broker, asset, CATEGORY_INTELLIGENCE)
        return [
            self.channel_layer.group_send(
                intel_group,
                {"type": "market.update", "payload": intel_payload},
            ),
        ]

    def _estimate_data_age(self, data: Any, category: str, now: float) -> Optional[float]:
        """
        Estimate how old the cached data is in milliseconds.
        Tries to use event timestamps from the data itself.
        Returns None when age cannot be determined (instead of float('inf')
        which breaks PostgreSQL JSON serialization).
        """
        if not data or not isinstance(data, (dict, list)):
            return None

        # Get the first item to check for timestamps
        item = data[0] if isinstance(data, list) else data
        if not isinstance(item, dict):
            return None

        # Try Binance event timestamps: 'E' (WS) or 'timestamp' (REST OI)
        ts = item.get("E") or item.get("timestamp") or item.get("ts")
        if ts:
            try:
                event_epoch_ms = float(ts)
                return max(0, (now * 1000) - event_epoch_ms)
            except (ValueError, TypeError):
                pass

        # For canonical trade data
        ts = item.get("T") or item.get("time")
        if ts:
            try:
                event_epoch_ms = float(ts)
                return max(0, (now * 1000) - event_epoch_ms)
            except (ValueError, TypeError):
                pass

        return None  # Unknown age — JSON-null instead of Infinity

    # ------------------------------------------------------------------
    # Error Broadcasting
    # ------------------------------------------------------------------

    async def _broadcast_error(self, underlying: str, message: str, category: str):
        """Notify connected clients about bridge errors."""
        try:
            error_payload = {
                "broker": self.broker,
                "category": CATEGORY_ERROR,
                "underlying": underlying,
                "data": {
                    "message": message,
                    "source_category": category,
                    "timestamp": int(time.time() * 1000),
                },
            }
            # Send to all groups for this underlying
            for cat in [CATEGORY_MARK_PRICE, CATEGORY_INTELLIGENCE, CATEGORY_TRADE]:
                group = ws_group_name(self.broker, underlying, cat)
                try:
                    await self.channel_layer.group_send(
                        group,
                        {"type": "market.update", "payload": error_payload},
                    )
                except Exception:
                    pass  # Best-effort error notification
        except Exception as e:
            logger.error(f"Failed to broadcast error notification: {e}")

    # ------------------------------------------------------------------
    # Throttle Cleanup
    # ------------------------------------------------------------------

    def _maybe_cleanup_throttle(self, now: float):
        """Remove entries from last_intel_time older than THROTTLE_CLEANUP_INTERVAL."""
        if (now - self.last_throttle_cleanup) < THROTTLE_CLEANUP_INTERVAL:
            return
        self.last_throttle_cleanup = now
        cutoff = now - THROTTLE_CLEANUP_INTERVAL
        stale_keys = [k for k, v in self.last_intel_time.items() if v < cutoff]
        for k in stale_keys:
            del self.last_intel_time[k]
        if stale_keys:
            logger.debug(
                f"Cleaned {len(stale_keys)} stale entries from intel throttle cache"
            )

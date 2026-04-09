"""
WebSocket Consumer for Market Data
====================================
Handles frontend WebSocket connections, subscription management, and data delivery.

Fixes applied:
  - Unsubscribe KeyError guard (discard instead of remove)
  - Disconnect reason logging
  - Input validation on group names
  - Heartbeat mechanism (server → client ping every 30s)
  - Proper group cleanup on disconnect
  - Instant push on subscribe for cached categories (indicators, intelligence, markPrice)
"""

import json
import asyncio
import logging
from channels.generic.websocket import AsyncWebsocketConsumer

from .broker_config import DEFAULT_BROKER, ws_group_name, ws_global_group, ws_universal_group, redis_key
from django.conf import settings

logger = logging.getLogger("market.consumer")

# Heartbeat interval in seconds
HEARTBEAT_INTERVAL = 30

# Allowed categories to prevent injection
ALLOWED_CATEGORIES = {
    "markPrice",
    "ticker",
    "trade",
    "open_interest",
    "intelligence",
    "index",
    "meta",
    "error",
    "indicators",
    "sessions",
}


class MarketConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        """Accept WebSocket connection, start heartbeat, auto-join global groups."""
        await self.accept()
        self.subscribed_groups = set()
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Auto-join universal channels (not per-symbol, no subscribe needed).
        # Sessions are broker-agnostic (Sydney/Tokyo/London/NY are the same
        # regardless of broker), so we use a universal group instead of
        # broker-specific groups. This ensures sessions reach the frontend
        # from ANY running orchestrator (Binance, Bybit, etc.).
        universal_sessions_group = ws_universal_group("sessions")
        await self.channel_layer.group_add(universal_sessions_group, self.channel_name)
        self.subscribed_groups.add(universal_sessions_group)

        logger.info(f"Frontend WS Connected: {self.channel_name}")

    async def disconnect(self, close_code):
        """Clean up: cancel heartbeat, leave all groups, log reason."""
        if hasattr(self, "heartbeat_task"):
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass

        for group in self.subscribed_groups:
            try:
                await self.channel_layer.group_discard(group, self.channel_name)
            except Exception:
                pass
        self.subscribed_groups.clear()

        # Log disconnect reason for debugging
        reason_map = {
            1000: "Normal closure",
            1001: "Going away",
            1006: "Abnormal closure (no close frame)",
            1011: "Internal error",
            1012: "Service restart",
            1013: "Try again later",
            4000: "Custom: rate limit",
            4001: "Custom: not authenticated",
        }
        reason = reason_map.get(close_code, f"Code {close_code}")
        logger.info(f"Frontend WS Disconnected: {self.channel_name} — {reason}")

    async def receive(self, text_data):
        """
        Handle commands from Frontend.
        Expected: {"action": "subscribe"|"unsubscribe", "underlying": "BTCUSDT", "category": "markPrice"}
        """
        try:
            data = json.loads(text_data)
            action = data.get("action")
            broker = data.get("broker", DEFAULT_BROKER)
            underlying = data.get("underlying", "").upper()
            category = data.get("category", "")

            if not underlying or not category:
                await self.send(
                    text_data=json.dumps(
                        {
                            "status": "error",
                            "message": "Missing underlying or category",
                        }
                    )
                )
                return

            # Input validation: reject unknown categories
            if category not in ALLOWED_CATEGORIES:
                await self.send(
                    text_data=json.dumps(
                        {
                            "status": "error",
                            "message": f"Unknown category: {category}",
                        }
                    )
                )
                return

            # Sanitize underlying: only allow alphanumeric + USDT suffix
            if (
                not underlying.replace("USDT", "")
                .replace("_", "")
                .replace("-", "")
                .isalnum()
            ):
                await self.send(
                    text_data=json.dumps(
                        {
                            "status": "error",
                            "message": "Invalid underlying format",
                        }
                    )
                )
                return

            group_name = ws_group_name(broker, underlying, category)

            if action == "subscribe":
                await self.channel_layer.group_add(group_name, self.channel_name)
                self.subscribed_groups.add(group_name)
                await self.send(
                    text_data=json.dumps(
                        {
                            "status": "subscribed",
                            "group": group_name,
                        }
                    )
                )

                # Instant push: serve cached data immediately on subscribe
                # so the frontend doesn't have to wait for the next candle/event.
                await self._push_cached_data(broker, underlying, category)

            elif action == "unsubscribe":
                # FIX: use discard instead of remove to prevent KeyError
                await self.channel_layer.group_discard(group_name, self.channel_name)
                self.subscribed_groups.discard(group_name)
                await self.send(
                    text_data=json.dumps(
                        {
                            "status": "unsubscribed",
                            "group": group_name,
                        }
                    )
                )

        except json.JSONDecodeError:
            await self.send(
                text_data=json.dumps(
                    {
                        "status": "error",
                        "message": "Invalid JSON",
                    }
                )
            )
        except Exception as e:
            logger.error(f"Consumer receive error: {e}", exc_info=True)

    async def market_update(self, event):
        """
        Channel Layer event handler.
        Triggered by RedisBridge's group_send. The 'type' must match this method name.
        """
        await self.send(text_data=json.dumps(event["payload"]))

    # ------------------------------------------------------------------
    # Instant Push on Subscribe
    # ------------------------------------------------------------------

    async def _push_cached_data(self, broker: str, underlying: str, category: str):
        """
        Push cached data from Redis immediately after subscribe.
        This eliminates the wait for the next candle/event cycle.

        Supported categories:
          - indicators: Redis Hash for values + Redis String for strategies
          - intelligence: raw JSON blob from redis_bridge
          - markPrice: raw JSON blob from redis_bridge
        """
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(
                settings.CACHES["default"]["LOCATION"], decode_responses=True
            )

            if category == "indicators":
                await self._push_indicators(r, broker, underlying)
            elif category in ("intelligence", "markPrice"):
                await self._push_raw_cached(r, broker, underlying, category)
            # Other categories (trade, ticker, oi) update too frequently
            # and are not critical on first load — skip instant push.

            await r.aclose()
        except Exception as e:
            logger.debug(f"Instant push failed for {underlying}/{category}: {e}")

    async def _push_indicators(self, r, broker: str, underlying: str):
        """Push cached indicator values + strategies from Redis."""
        values_key = redis_key(broker, underlying, "indicators")
        strategies_key = redis_key(broker, underlying, "indicators:strategies")

        # Fetch values (Hash) and strategies (String) in parallel
        values_raw, strategies_json = await asyncio.gather(
            r.hgetall(values_key),
            r.get(strategies_key),
        )

        if not values_raw:
            return  # No cached indicators yet

        values = {k: float(v) for k, v in values_raw.items() if v is not None}
        strategies = []
        if strategies_json:
            try:
                strategies = json.loads(strategies_json)
            except (json.JSONDecodeError, TypeError):
                pass

        payload = {
            "category": "indicators",
            "underlying": underlying,
            "broker": broker,
            "data": {"values": values, "strategies": strategies},
        }
        await self.send(text_data=json.dumps(payload))
        logger.debug(f"Instant push indicators for {underlying} ({len(values)} values)")

    async def _push_raw_cached(self, r, broker: str, underlying: str, category: str):
        """Push a raw cached JSON blob (intelligence, markPrice)."""
        cache_key = redis_key(broker, underlying, category)
        raw = await r.get(cache_key)
        if not raw:
            return

        try:
            payload = json.loads(raw)
            await self.send(text_data=json.dumps(payload))
            logger.debug(f"Instant push {category} for {underlying}")
        except (json.JSONDecodeError, TypeError):
            pass

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self):
        """Send periodic pings to detect dead connections."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await self.send(
                    text_data=json.dumps(
                        {
                            "type": "ping",
                            "timestamp": __import__("time").time(),
                        }
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Heartbeat ended for {self.channel_name}: {e}")

# backend/apps/common/consumers.py

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
"""

import json
import asyncio
import logging
from channels.generic.websocket import AsyncWebsocketConsumer

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
}


class MarketConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        """Accept WebSocket connection and start heartbeat."""
        await self.accept()
        self.subscribed_groups = set()
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
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

            group_name = f"{underlying}_{category}".replace("-", "_")

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

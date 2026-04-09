"""
Options WebSocket Connector
============================
Connects to broker Options WebSocket streams, routes events to RedisBridge.
Reads all WS URLs from broker_config.py — no hardcoded values.

Fixes applied:
  - Async Redis only (no sync Redis in __init__)
  - WebSocket ping/pong keepalive
  - Exponential backoff reconnection
  - Proper error propagation
"""

import asyncio
import json
import logging
import websockets

from .redis_bridge import (
    RedisBridge,
    CATEGORY_MARK_PRICE,
    CATEGORY_TICKER,
    CATEGORY_TRADE,
    CATEGORY_OPEN_INTEREST,
    CATEGORY_INDEX,
)
from .broker_config import get_broker_config, redis_global_key, DEFAULT_BROKER

logger = logging.getLogger("options.connector")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Reconnection backoff: 5s → 10s → 20s → 40s → 60s (capped)
RECONNECT_BASE_DELAY = 5.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_MULTIPLIER = 2.0

# WebSocket keepalive
WS_PING_INTERVAL = 20  # seconds between pings
WS_PING_TIMEOUT = 10  # seconds to wait for pong
WS_CLOSE_TIMEOUT = 5  # seconds for close handshake
WS_OPEN_TIMEOUT = 10  # seconds to wait for WebSocket handshake to complete


class OptionsConnector:
    """
    Broker-agnostic Options WebSocket connector.
    All WS URLs are read from broker_config.py.
    """

    def __init__(self, broker: str = DEFAULT_BROKER, testnet: bool = False):
        self.bridge = RedisBridge(broker=broker)
        self.broker = broker
        self.testnet = testnet

        # Read WS URLs from broker_config
        config = get_broker_config(broker)
        options = config["options"]

        if testnet:
            self.ws_market_base = options.get(
                "ws_market_base_testnet", options["ws_market_base"]
            )
            self.ws_public_base = options.get(
                "ws_public_base_testnet", options["ws_public_base"]
            )
        else:
            self.ws_market_base = options["ws_market_base"]
            self.ws_public_base = options["ws_public_base"]

    async def stop(self):
        """Gracefully close all underlying connections."""
        logger.info(f"Stopping {self.broker.title()} Connector and closing Redis connections...")
        await self.bridge.close()

    async def _read_exchange_info_async(self) -> dict:
        """Read exchange info from Redis using async client (avoids blocking event loop)."""
        import redis.asyncio as aioredis
        from django.conf import settings

        r = aioredis.from_url(
            settings.CACHES["default"]["LOCATION"],
            decode_responses=True,
        )
        try:
            raw = await r.get(redis_global_key(self.broker, "exchange_info"))
            if raw:
                return json.loads(raw)
            return {}
        finally:
            await r.aclose()

    async def start_market_streams(self, underlyings: list):
        """Prepare and consume market data streams."""
        logger.info("Preparing Market Streams...")
        streams = ["!index@arr"]

        # Async read of exchange info (no sync Redis in event loop)
        ext_info = await self._read_exchange_info_async()
        asset_expiries = {}

        if ext_info:
            for s in ext_info.get("optionSymbols", []):
                u = s["underlying"]
                if u in underlyings:
                    from datetime import datetime

                    expiry_dt = datetime.fromtimestamp(s["expiryDate"] / 1000)
                    expiry_str = expiry_dt.strftime("%y%m%d")
                    if u not in asset_expiries:
                        asset_expiries[u] = set()
                    asset_expiries[u].add(expiry_str)

        for u in underlyings:
            u_l = u.lower()
            streams.append(f"{u_l}@optionMarkPrice")
            streams.append(f"{u_l}@optionTrade")

            # Subscribe to OI for the Top 3 nearest expirations only
            expiries = sorted(list(asset_expiries.get(u, [])))
            for exp in expiries[:3]:
                streams.append(f"{u_l}@optionOpenInterest@{exp}")

        uri = f"{self.ws_market_base}/stream?streams={'/'.join(streams)}"
        await self._consume(uri, "market")

    async def start_public_streams(self, underlyings: list):
        """Handle 24hr Tickers via /public base path."""
        logger.info("Preparing Public Streams...")
        streams = []
        for u in underlyings:
            streams.append(f"{u.lower()}@optionTicker")

        uri = f"{self.ws_public_base}/stream?streams={'/'.join(streams)}"
        await self._consume(uri, "public")

    async def _consume(self, uri: str, stream_type: str):
        """
        Consume a WebSocket stream with:
          - ping/pong keepalive
          - exponential backoff reconnection
          - error logging
        """
        logger.info(f"Initiating {stream_type} WebSocket connection...")
        delay = RECONNECT_BASE_DELAY

        while True:
            try:
                logger.info(
                    f"Connecting to {self.broker.title()} {stream_type} stream "
                    f"(next retry in {delay:.1f}s if fails)..."
                )
                async with websockets.connect(
                    uri,
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                    close_timeout=WS_CLOSE_TIMEOUT,
                    open_timeout=WS_OPEN_TIMEOUT,
                ) as ws:
                    logger.info(
                        f"SUCCESS: Connected to {self.broker.title()} {stream_type} stream."
                    )
                    delay = RECONNECT_BASE_DELAY  # Reset on successful connection
                    msg_count = 0

                    while True:
                        msg = await ws.recv()
                        packet = json.loads(msg)
                        msg_count += 1
                        stream_name = packet.get("stream")
                        data = packet.get("data")

                        if msg_count % 100 == 0:
                            logger.debug(
                                f"{stream_type} stream: {msg_count} messages received"
                            )

                        await self._route_event(data, stream_name)

            except asyncio.CancelledError:
                # Graceful shutdown — don't reconnect, let the caller clean up
                logger.info(f"{stream_type} stream task cancelled. Shutting down.")
                raise
            except websockets.ConnectionClosed as e:
                logger.warning(f"{stream_type} WS closed (code={e.code}): {e.reason}")
            except websockets.InvalidStatusCode as e:
                logger.error(f"{stream_type} WS invalid status: {e.status_code}")
            except Exception as e:
                logger.error(f"{stream_type} WS error: {e}", exc_info=True)

            # Exponential backoff (only reached on errors, not on cancellation)
            logger.warning(f"Reconnecting {stream_type} in {delay:.1f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * RECONNECT_MULTIPLIER, RECONNECT_MAX_DELAY)

    async def _route_event(self, data, stream_name: str):
        """Route incoming WebSocket events to the bridge for broadcasting."""
        if not data:
            return

        first_item = data[0] if isinstance(data, list) else data
        event_type = first_item.get("e")
        logger.debug(f"Incoming Event: {event_type} from {stream_name}")

        underlying = stream_name.split("@")[0].upper() if stream_name else "GLOBAL"

        if event_type == "indexPrice":
            if isinstance(data, list):
                for item in data:
                    await self.bridge.broadcast(
                        CATEGORY_INDEX, item["s"], item, stream_name
                    )
            else:
                await self.bridge.broadcast(
                    CATEGORY_INDEX, first_item["s"], data, stream_name
                )

        elif event_type in [
            "markPrice",
            "24hrTicker",
            "ticker",
            "trade",
            "openInterest",
        ]:
            category_map = {
                "markPrice": CATEGORY_MARK_PRICE,
                "24hrTicker": CATEGORY_TICKER,
                "ticker": CATEGORY_TICKER,
                "trade": CATEGORY_TRADE,
                "openInterest": CATEGORY_OPEN_INTEREST,
            }
            category = category_map.get(event_type)

            # OI events use symbol format: BTC-250328-90000-P → underlying = BTCUSDT
            if event_type == "openInterest":
                symbol = first_item.get("s", "")
                underlying = symbol.split("-")[0].upper() + "USDT"

            await self.bridge.broadcast(category, underlying, data, stream_name)


# Backward-compatible aliases — existing code uses BinanceOptionsConnector
BinanceOptionsConnector = OptionsConnector
